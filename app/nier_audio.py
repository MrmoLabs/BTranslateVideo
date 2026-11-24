"""Read-only audio catalogue and stream MOD packaging for NieR:Automata PC."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
from zipfile import ZIP_DEFLATED, ZipFile

from .media import check_cancel


LANGUAGES = {'ja': 'Japanese', 'en': 'English(US)'}
STREAM_INDEX = 'WwiseStreamInfo.wai'
WAI_MAGIC = 0x20170412
ALIGNMENT = 2048
MAX_WEM = 32 * 1024 * 1024


@dataclass(frozen=True)
class StreamEntry:
    media_id: int
    size: int
    offset: int
    name_index: int
    pack_index: int
    directory: str
    path: str


def safe_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(relative)
    return path


def sound_root(path: str) -> Path:
    root = Path(path).expanduser()
    if not root.is_absolute() or not (root / 'NieRAutomata.exe').is_file():
        raise ValueError('请选择包含 NieRAutomata.exe 的游戏安装目录')
    sound = root.resolve() / 'data' / 'sound'
    safe_file(sound, STREAM_INDEX)
    return sound


def stream_index(data: bytes) -> tuple[list[bytes], list[bytes], list[StreamEntry]]:
    if len(data) < 16:
        raise ValueError('流式语音索引太短')
    magic, directory_count, name_count, count = struct.unpack_from('<4I', data)
    start = 16 + 32 * directory_count
    records_offset = start + 32 * name_count
    if magic != WAI_MAGIC or records_offset + 16 * count != len(data):
        raise ValueError('不支持此版本的 WwiseStreamInfo.wai')
    directories = [data[i:i + 32] for i in range(16, start, 32)]
    names = [data[i:i + 32] for i in range(start, records_offset, 32)]
    entries = []
    for directory in directories:
        name = directory[:16].rstrip(b'\0').decode('ascii')
        first, stop = struct.unpack_from('<II', directory, 24)
        if name not in {'', *LANGUAGES.values()} or first != len(entries) or not first <= stop <= count:
            raise ValueError('不支持此语音索引的语言或编号布局')
        for index in range(first, stop):
            media, size, offset, name_id, pack_id = struct.unpack_from('<IIIHH', data, records_offset + 16 * index)
            if name_id >= len(names):
                raise ValueError('语音包名称索引超出范围')
            stem = names[name_id][:16].rstrip(b'\0').decode('ascii')
            if not re.fullmatch(r'[a-zA-Z0-9_]+', stem):
                raise ValueError('无效语音包名称')
            pack = f'{stem}_{pack_id // 1000}_{pack_id % 1000:03}.wsp'
            path = '/'.join(filter(None, ('stream', name, pack)))
            entries.append(StreamEntry(media, size, offset, name_id, pack_id, name, path))
    if len(entries) != count:
        raise ValueError('语音索引存在未分组的条目')
    return directories, names, entries


def bank_chunks(data: bytes):
    pos = 0
    view = memoryview(data)
    while pos + 8 <= len(data):
        name, size = struct.unpack_from('<4sI', data, pos)
        start = pos + 8
        pos = start + size
        if pos > len(data):
            raise ValueError('声音库包含越界的区块')
        yield name, start, view[start:pos]
    if pos != len(data):
        raise ValueError('声音库区块不完整')


def sound_objects(data: memoryview):
    count, = struct.unpack_from('<I', data)
    pos = 4
    for _ in range(count):
        kind, size, object_id = struct.unpack_from('<BII', data, pos)
        end = pos + 5 + size
        if size < 4 or end > len(data):
            raise ValueError('声音库事件条目不完整')
        yield kind, object_id, data[pos + 9:end]
        pos = end
    if pos != len(data):
        raise ValueError('声音库事件计数不匹配')


def read_bank(path: Path):
    chunks = {name: (start, chunk) for name, start, chunk in bank_chunks(path.read_bytes())}
    if b'BKHD' not in chunks or struct.unpack_from('<I', chunks[b'BKHD'][1])[0] != 113:
        raise ValueError(f'不支持声音库版本 {path.name}')
    embedded = {}
    if b'DIDX' in chunks and b'DATA' in chunks:
        start, content = chunks[b'DATA']
        for media, offset, length in struct.iter_unpack('<III', chunks[b'DIDX'][1]):
            if offset + length > len(content):
                raise ValueError('内嵌语音超出声音库区块')
            embedded[media] = (start + offset, length)
    sounds, actions, events = {}, {}, defaultdict(list)
    if b'HIRC' in chunks:
        for kind, object_id, body in sound_objects(chunks[b'HIRC'][1]):
            if kind == 2 and len(body) >= 9:
                sounds[object_id] = struct.unpack_from('<I', body, 5)[0]
            elif kind == 3 and len(body) >= 6 and body[1] == 4:
                actions[object_id] = struct.unpack_from('<I', body, 2)[0]
            elif kind == 4 and len(body) >= 4 and len(body) == 4 + 4 * struct.unpack_from('<I', body)[0]:
                for action, in struct.iter_unpack('<I', body[4:]):
                    events[action].append(object_id)
    direct = defaultdict(set)
    for action, sound in actions.items():
        if sound in sounds:
            direct[sound].update(events[action])
    return embedded, sounds, direct


def locator(path, media, offset, size, *, stream):
    return dict(path=path, media=media, offset=offset, size=size, stream=stream)


def catalogue(sound: Path, language: str):
    raw = safe_file(sound, STREAM_INDEX).read_bytes()
    _, _, entries = stream_index(raw)
    voices = {name: {} for name in LANGUAGES.values()}
    links = defaultdict(dict)
    events = defaultdict(set)
    duplicate_ids = Counter(entry.media_id for entry in entries)
    sizes = {}
    for entry in entries:
        if entry.directory not in voices:
            continue
        if entry.path not in sizes:
            sizes[entry.path] = safe_file(sound, entry.path).stat().st_size
        if entry.offset + entry.size > sizes[entry.path] or not 12 <= entry.size <= MAX_WEM:
            raise ValueError(f'语音索引与 {entry.path} 不匹配')
        voices[entry.directory][entry.media_id] = locator(entry.path, entry.media_id, entry.offset, entry.size, stream=True)

    prefetch = defaultdict(set)
    for directory, media in voices.items():
        for bank in sorted((sound / directory).glob('vo_*.bnk')):
            embedded, sounds, direct = read_bank(safe_file(sound, f'{directory}/{bank.name}'))
            for media_id, (offset, size) in embedded.items():
                if media_id in media and media[media_id]['stream']:
                    prefetch[directory].add(media_id)
                media.setdefault(media_id, locator(f'{directory}/{bank.name}', media_id, offset, size, stream=False))
            for object_id, media_id in sounds.items():
                if media_id in media:
                    links[(bank.name, object_id)][directory] = media_id
                    events[(directory, media_id)].update(direct[object_id])

    source = LANGUAGES[language]
    other_language = 'en' if language == 'ja' else 'ja'
    other = LANGUAGES[other_language]
    proposals = defaultdict(set)
    reverse = defaultdict(set)
    for pair in links.values():
        if source in pair and other in pair:
            proposals[pair[source]].add(pair[other])
            reverse[pair[other]].add(pair[source])
    rows = []
    for media_id, asset in sorted(voices[source].items(), key=lambda row: (row[1]['path'], row[1]['offset'])):
        related = proposals[media_id]
        matches = [item for item in related if reverse[item] == {media_id}]
        assets = {language: asset}
        if len(related) == len(matches) == 1:
            assets[other_language] = voices[other][matches[0]]
        _, channels, rate = asset_format(sound, asset)
        rows.append(dict(id=str(media_id), group=Path(asset['path']).name, assets=assets,
                         channels=channels, rate=rate, events=sorted(events[source, media_id]),
                         replaceable=asset['stream'] and media_id not in prefetch[source] and duplicate_ids[media_id] == 1))
    return dict(index_hash=hashlib.sha256(raw).hexdigest(), entries=rows)


def asset_format(sound: Path, asset: dict):
    with safe_file(sound, asset['path']).open('rb') as stream:
        stream.seek(asset['offset'])
        head = stream.read(256)
    return wem_header(head)


def extract_wem(sound: Path, asset: dict) -> bytes:
    if not 12 <= asset['size'] <= MAX_WEM:
        raise ValueError('WEM 大小超出支持范围')
    with safe_file(sound, asset['path']).open('rb') as stream:
        stream.seek(asset['offset'])
        data = stream.read(asset['size'])
    if len(data) != asset['size'] or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('找不到完整的 Wwise 音频，游戏资源可能已变化')
    return data


def wem_header(data: bytes):
    """Read codec/channels/rate from a WEM header without loading the body."""
    if len(data) < 36 or data[:4] != b'RIFF' or data[8:16] != b'WAVEfmt ':
        raise ValueError('无效的 Wwise WEM 文件')
    fmt_size, = struct.unpack_from('<I', data, 16)
    codec, channels, rate = struct.unpack_from('<HHI', data, 20)
    if not 16 <= fmt_size <= len(data) - 20 or not 1 <= channels <= 8 or not rate:
        raise ValueError('Wwise WEM 文件头不完整')
    return codec, channels, rate


def wem_format(data: bytes):
    codec, channels, rate = wem_header(data)
    riff_size, = struct.unpack_from('<I', data, 4)
    if riff_size + 8 > len(data):
        raise ValueError('Wwise WEM 文件不完整')
    return codec, channels, rate


def make_mod(sound, index_hash, language, name, replacements, destination, cancel, *, build='', release=False):
    """Build a manager ZIP. WSP members contain only the replacement WEMs."""
    index_bytes = safe_file(sound, STREAM_INDEX).read_bytes()
    if hashlib.sha256(index_bytes).hexdigest() != index_hash:
        raise ValueError('游戏语音索引已变化，请使用原来的游戏版本或新建项目重新扫描')
    directories, names, entries = stream_index(index_bytes)
    target = LANGUAGES[language]
    counts = Counter(entry.media_id for entry in entries)
    selected = [entry for entry in entries if entry.directory == target and entry.media_id in replacements]
    if not selected or len(selected) != len(replacements) or any(counts[entry.media_id] != 1 for entry in selected):
        raise ValueError('所选条目无法唯一定位到目标语音档')

    # Retain the source name table indices used by the manager when merging into the game.
    positions = {}
    groups = defaultdict(list)
    for entry in selected:
        groups[entry.path].append(entry)
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED, compresslevel=3, allowZip64=False) as archive:
        for pack, members in groups.items():
            offset = 0
            with archive.open(pack, 'w') as stream:
                for entry in members:
                    check_cancel(cancel)
                    path = replacements[entry.media_id]['wem']
                    data = path.read_bytes()
                    codec, _, _ = wem_format(data)
                    if codec != 0xffff or len(data) > MAX_WEM:
                        raise ValueError(f'资源 {entry.media_id} 需要有效的 Wwise Vorbis WEM')
                    stream.write(data)
                    positions[entry.media_id] = (len(data), offset)
                    padding = -len(data) % ALIGNMENT
                    stream.write(bytes(padding))
                    offset += len(data) + padding

        table = bytearray(struct.pack('<4I', WAI_MAGIC, len(directories), len(names), len(selected)))
        cursor = 0
        for directory in directories:
            count = sum(entry.directory.encode('ascii') == directory[:16].rstrip(b'\0') for entry in selected)
            table.extend(directory[:24] + struct.pack('<II', cursor, cursor + count))
            cursor += count
        table.extend(b''.join(names))
        for entry in selected:
            size, offset = positions[entry.media_id]
            table.extend(struct.pack('<IIIHH', entry.media_id, size, offset, entry.name_index, entry.pack_index))
        archive.writestr(STREAM_INDEX, table)
        changed = {str(entry.media_id): dict(id=entry.media_id, name=replacements[entry.media_id]['label']) for entry in selected}
        archive.writestr('audioModsMetadata.json', json.dumps(dict(version='1.1.0', name=name,
            moddedWaiChunks=changed, moddedWaiEventChunks={}, moddedBnkChunks={}), ensure_ascii=False))
        archive.writestr('manifest.json', json.dumps(dict(schema=1, game='NieR:Automata', name=name, game_build=build,
            voice_slot=target, original_index_sha256=index_hash, test=not release,
            entries=[dict(id=entry.media_id, package=entry.path, label=replacements[entry.media_id]['label'],
                          tested=replacements[entry.media_id]['tested']) for entry in selected]), ensure_ascii=False))
        archive.writestr('README.txt', f'{name}\n\n使用兼容的 Music Mod Manager 或 F-SERVO 导入此 ZIP。\n'
                         f'安装后在游戏里选择 {target} 语音。退出游戏后由管理器卸载。\n'
                         '适配版本、覆盖条目和实机验收状态见 manifest.json。\n')
