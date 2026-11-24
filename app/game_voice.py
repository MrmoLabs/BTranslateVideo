"""Persistent game voice projects, audition caches, and shared model jobs."""
from __future__ import annotations

from array import array
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Event, RLock
from types import SimpleNamespace
import uuid
import wave
from xml.etree import ElementTree
from zipfile import ZipFile

from fastapi import HTTPException

from .audio_cache import reusable_voice_clip
from .config import ROOT, atomic_json, resolve, separation_python
from .emotions import PRESETS, emotion_fields
from .media import Cancelled, check_cancel, extract_audio, ffmpeg, run, wav_duration
from .model_backends import transcribe, tts_backend, tts_model_signature, validate_tts
from .nier_audio import catalogue, extract_wem, make_mod, safe_file, wem_format
from .nier_text import dialogue_lines, dialogue_source
from .pipeline import Pipeline, file_hash, fingerprint, make_voice_clip, offline_env
from .store import now
from .translation import local_translator, translate_batch
from .translation_quality import polish_translation, translation_context
from . import drama3, voice_conversion


ACTIVE = {'queued', 'running', 'cancelling'}
MAX_GUIDE_BYTES = 32 * 1024 * 1024
BANDIT_SOURCE = ROOT / 'runtime' / 'bandit-infer-source' / 'src'
BANDIT_CHECKPOINT = ROOT / 'models' / 'bandit-v2' / 'checkpoint-multi.ckpt'
BANDIT_SHA256 = 'abcfccf65446752a057f4a302c941479a54b7560ebf8d7bca039d2ea98e64cfc'
TUNING = dict(speed=1, gain=0, trim_start=0, trim_end=0, lead=0, tail=0)
VOICE_FIELDS = ('text', 'voice', 'role', 'emotion', 'emotion_strength', 'preserve_background', 'background_backend', 'target_duration', *TUNING)
DRAMA_FIELDS = ('drama3_direction', 'drama3_reference_text')
DRAMA_ROUTES = {'drama3', 'drama3_conversion'}
DEFAULT_ROW = dict(ja='', en='', ja_source='', en_source='', text='', text_source='', role='', scene='',
                   notes='', voice='', emotion='inherit', emotion_strength='medium', preserve_background=False,
                   tts_fit='', keep_original=False, scene_order=0, target_duration=0, background_backend='demucs',
                   drama3_direction='', drama3_reference_text='', **TUNING)
RISKY_FIT = {'short', 'shout', 'mixed'}
DEFAULT_PRONUNCIATION = [{'term': '2B', 'spoken': 'two b'}]
NON_SPEECH_NOTES = (
    'プログラムでランダムな記号を表示', '随机显示符号', '隨機顯示符號',
)


def is_sound_note(row):
    return any(note in row.get(field, '') for field in ('ja', 'en', 'text') for note in NON_SPEECH_NOTES)


def voice_for(row, default_voice, role_voices):
    """Resolve a line voice: per-line override, role mapping, batch default."""
    return row.get('voice') or role_voices.get(row.get('role') or '') or default_voice


def playback_decision(row):
    if row.get('keep_original'):
        return 'original'
    return 'chinese' if row.get('selected') else 'pending'


def bandit_available():
    return (separation_python().is_file() and BANDIT_CHECKPOINT.is_file()
            and (BANDIT_SOURCE / 'bandit_infer' / '__init__.py').is_file())


def generation_settings(row, voice, role_voices, emotion, force_batch_settings, vc=False):
    if force_batch_settings:
        voice_id = voice
    elif vc:
        # Voice conversion: the line/batch voice is the native Chinese source,
        # while role_voices holds the original character timbre to convert to.
        voice_id = row.get('voice') or voice
    else:
        voice_id = voice_for(row, voice, role_voices)
    expression = (emotion or 'inherit') if force_batch_settings or row['emotion'] == 'inherit' else row['emotion']
    return dict(voice=voice_id, emotion=expression)


def script_signature(row):
    return fingerprint([row.get(key, '') for key in ('ja', 'en', 'text', 'role', 'scene')])


def fit_flags(path):
    """Flag lines that are poor TTS targets, from the original decoded audio.

    ``short`` = too little speech to clone/prosody; ``shout`` = loud/high-energy
    delivery; ``mixed`` = noisy, non-solo or very long audio. Thresholds were
    calibrated on real NieR voice lines; ``mixed`` is a hint, not a certainty.
    """
    import numpy as np
    with wave.open(str(path), 'rb') as stream:
        rate = stream.getframerate()
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
    duration = len(samples) / rate if rate else 0
    flags = set()
    if duration < .7:
        flags.add('short')
    if duration > 20:
        flags.add('mixed')
        return flags
    frame, hop = int(rate * .04), int(rate * .02)
    rms_values, f0_values, cent_values, flat_values, harm_values, zcr_values = [], [], [], [], [], []
    for i in range(0, len(samples) - frame, hop):
        segment = samples[i:i + frame]
        segment = segment - segment.mean()
        rms = float(np.sqrt((segment ** 2).mean()))
        if rms < .008:
            continue
        rms_values.append(rms)
        windowed = segment * np.hanning(frame)
        spectrum = np.abs(np.fft.rfft(windowed)) + 1e-12
        freqs = np.fft.rfftfreq(frame, 1 / rate)
        cent_values.append(float((spectrum * freqs).sum() / spectrum.sum()))
        flat_values.append(float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum)))
        zcr_values.append(float(np.mean(np.abs(np.diff(np.sign(segment))) > 0)))
        corr = np.correlate(segment, segment, 'full')[frame - 1:]
        lo, hi = int(rate / 420), int(rate / 60)
        if hi < len(corr):
            region = corr[lo:hi]
            peak = int(np.argmax(region))
            harm_values.append(float(region[peak] / (corr[0] + 1e-9)))
            if region[peak] > .3 * corr[0]:
                f0_values.append(rate / (lo + peak))

    def median(values, default=0.0):
        return float(np.median(values)) if values else default

    rms, f0, cent = median(rms_values), median(f0_values), median(cent_values)
    flat, harm, zcr = median(flat_values), median(harm_values), median(zcr_values)
    if rms >= .16 or (rms >= .115 and (cent >= 2800 or f0 >= 340)) or (rms >= .05 and cent >= 4800):
        flags.add('shout')
    if zcr >= .18 or (flat >= .11 and harm <= .42):
        flags.add('mixed')
    return flags


def spoken_text(text, rules):
    words = {rule['term'].lower(): rule['spoken'] for rule in rules}
    if not words:
        return text
    terms = '|'.join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    pattern = rf'(?<![a-z0-9_])(?:{terms})(?![a-z0-9_])'
    return re.sub(pattern, lambda match: words[match.group().lower()], text, flags=re.IGNORECASE | re.ASCII)


def project_id(value):
    if not re.fullmatch(r'[a-f0-9]{32}', value):
        raise FileNotFoundError(value)
    return value


def audio_info(path):
    with wave.open(str(path), 'rb') as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            raise ValueError('试听缓存需要单声道 16 位 PCM WAV')
        frames = stream.getnframes()
        width = max(1, math.ceil(frames / 420))
        peaks = []
        while chunk := stream.readframes(width):
            samples = array('h', chunk)
            if sys.byteorder != 'little':
                samples.byteswap()
            peaks.append(round(max(map(abs, samples)) / 32768, 4))
        return dict(duration=frames / stream.getframerate(), peaks=peaks, peak=max(peaks, default=0))


def tools():
    root = ROOT / 'runtime/game-audio-tools'

    def find(env, executable, candidates):
        path = os.environ.get(env) or shutil.which(executable)
        options = [Path(path).expanduser()] if path else candidates
        return next((item.resolve() for item in options if item.is_file()), None)

    decoder = find('BTRANSLATE_VGMSTREAM', 'vgmstream-cli', [root / 'vgmstream/vgmstream-cli.exe'])
    legacy = list((root / 'wwise-2014').glob('portable/**/WwiseCLI.exe'))
    for base in filter(None, (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'))):
        legacy.extend(Path(base).glob('Audiokinetic/Wwise*2014*/Authoring/*/Release/bin/WwiseCLI.exe'))
    encoder = find('BTRANSLATE_WWISE_CLI', 'WwiseCLI.exe', legacy)
    template = Path(os.environ.get('BTRANSLATE_WWISE_TEMPLATE', root / 'wav-to-wem-template.zip')).expanduser()
    return decoder, encoder, template.resolve() if template.is_file() else None


def encode_wem(encoder, template, wav_path, output, log, cancel):
    if not encoder or not template:
        raise ValueError('打包需要 Wwise 2014 Authoring CLI 和语音转换模板，请查看工作台的工具状态')
    with TemporaryDirectory(prefix='wwise-', dir=output.parent) as temporary:
        folder = Path(temporary)
        with ZipFile(template) as archive:
            for member in archive.infolist():
                if not (folder / member.filename).resolve().is_relative_to(folder.resolve()):
                    raise ValueError('Wwise 模板包含越界路径')
            archive.extractall(folder)
        project = safe_file(folder, 'wavToWemTemplate.wproj')
        source = folder / 'wavSrc'
        source.mkdir(exist_ok=True)
        shutil.copyfile(wav_path, source / 'new-voice.wav')
        request = ElementTree.Element('ExternalSourcesList', SchemaVersion='1', Root='wavSrc')
        ElementTree.SubElement(request, 'Source', Path=str(source / 'new-voice.wav'), Conversion='External_HighQuality')
        request_path = source / 'ExtSourceList.wsources'
        ElementTree.ElementTree(request).write(request_path, encoding='utf-8', xml_declaration=True)
        # Wwise 2014 returns 2 for successful conversions with warnings.
        run([encoder, project, '-ConvertExternalSources', 'Windows', request_path, '-NoWwiseDat', '-Verbose'],
            log, cancel, cwd=folder, timeout=120, success_codes=(0, 2))
        check_cancel(cancel)
        converted = safe_file(folder, 'GeneratedSoundBanks/Windows/new-voice.wem')
        if wem_format(converted.read_bytes())[0] != 0xffff:
            raise ValueError('Wwise 模板未生成 Vorbis WEM')
        shutil.copyfile(converted, output)


class GameVoiceProjects:
    def __init__(self, root, pipeline, voices):
        self.root, self.pipeline, self.voices = root, pipeline, voices
        self.lock = RLock()
        self.catalogues = {}

    def folder(self, identifier):
        return self.root / project_id(identifier)

    def state(self, identifier):
        with self.lock:
            return json.loads((self.folder(identifier) / 'project.json').read_text(encoding='utf-8'))

    def write(self, identifier, state):
        with self.lock:
            atomic_json(self.folder(identifier) / 'project.json', state)

    def create(self, title, sound, language):
        title = title.strip()
        if not title:
            raise ValueError('请输入项目名称')
        scan = catalogue(sound, language)
        if not scan['entries']:
            raise ValueError('目标语音档没有找到音频')
        identifier = uuid.uuid4().hex
        folder = self.folder(identifier)
        (folder / 'cache').mkdir(parents=True)
        index = sound.parent.parent / 'Latest_BuildInfo.properties'
        build = index.read_text(encoding='utf-8-sig', errors='replace').strip()[:400] if index.is_file() else ''
        atomic_json(folder / 'catalogue.json', scan['entries'])
        self.write(identifier, dict(id=identifier, title=title, sound=str(sound), language=language, build=build,
            index_hash=scan['index_hash'], rows={}, revision=0, packages=[], job={}, role_voices={},
            pronunciation=DEFAULT_PRONUNCIATION, updated=now()))
        return self.detail(identifier)

    def import_text(self, identifier, revision, *, overwrite=False):
        """Fill blank lines from the bundled game-text lookup.

        Import is explicit and non-destructive: existing source, translation,
        role and tuning fields stay untouched unless ``overwrite`` is true.
        """
        with self.pipeline.lock, self.lock:
            data = self.editable(identifier, revision)
            entries = self.entries(identifier)
            lookup = dialogue_lines()
            matched = changed = chinese = 0
            for row_id, _entry in entries.items():
                item = lookup.get(str(row_id))
                if not item:
                    continue
                matched += 1
                stored = data['rows'].get(row_id, {})
                patch = {}
                for key, source_key in (('ja', 'ja'), ('en', 'en'), ('text', 'zh')):
                    value = (item.get(source_key) or '').strip()
                    if value and (overwrite or not stored.get(key)):
                        patch[key] = value
                        patch['text_source' if key == 'text' else key + '_source'] = 'game'
                role = (item.get('role') or '').strip()
                if role and (overwrite or not stored.get('role')):
                    patch['role'] = role
                if patch.get('text') and stored.get('selected'):
                    patch.update(selected='', tested=False)
                if patch:
                    data['rows'][row_id] = {**stored, **patch}
                    changed += 1
                    if patch.get('text'):
                        chinese += 1
            if changed:
                data['revision'] += 1
                data['updated'] = now()
                self.write(identifier, data)
            return dict(matched=matched, filled=changed, chinese=chinese, total=len(entries),
                        revision=data['revision'], source=dialogue_source())

    def entries(self, identifier):
        with self.lock:
            if identifier not in self.catalogues:
                entries = json.loads((self.folder(identifier) / 'catalogue.json').read_text(encoding='utf-8'))
                if len(self.catalogues) >= 4:
                    self.catalogues.pop(next(iter(self.catalogues)))
                self.catalogues[identifier] = {row['id']: row for row in entries}
            return self.catalogues[identifier]

    def detail(self, identifier):
        data = self.state(identifier)
        entries = self.entries(identifier)
        rows = data['rows'].values()
        return dict(id=identifier, title=data['title'], language=data['language'], build=data['build'],
            revision=data['revision'], job=data['job'], packages=list(reversed(data['packages'])), total=len(entries),
            pronunciation=data.get('pronunciation', DEFAULT_PRONUNCIATION), role_voices=data.get('role_voices', {}),
            replaceable=sum(row['replaceable'] for row in entries.values()),
            ready=sum(playback_decision(row) == 'chinese' for row in rows),
            original=sum(playback_decision(row) == 'original' for row in rows),
            transcribed=sum(bool(row.get('ja') or row.get('en')) for row in rows),
            translated=sum(bool(row.get('text')) for row in rows),
            tested=sum(bool(row.get('tested')) and playback_decision(row) == 'chinese' for row in rows), updated=data['updated'],
            groups=sorted({row['group'] for row in entries.values()}),
            roles=sorted({row['role'] for row in rows if row.get('role')}),
            scenes=sorted({row['scene'] for row in rows if row.get('scene')}),
            vc=bool(data.get('vc')))

    def list(self):
        projects = []
        for path in self.root.glob('*/project.json'):
            try:
                projects.append(self.detail(path.parent.name))
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda row: row['updated'], reverse=True)

    def recover(self):
        for path in self.root.glob('*/project.json'):
            try:
                data = self.state(path.parent.name)
            except (OSError, ValueError):
                continue
            if data['job'].get('status') in ACTIVE:
                data['job'].update(status='interrupted', phase='上次任务中断，已完成内容已保留')
                self.write(data['id'], data)

    def row(self, identifier, row_id, data=None):
        entry = self.entries(identifier).get(row_id)
        if entry is None:
            raise FileNotFoundError(row_id)
        data = data if data is not None else self.state(identifier)
        stored = data['rows'].get(row_id, {})
        row = dict(**entry, **{**DEFAULT_ROW, 'candidates': {}, 'selected': '', 'tested': False, **stored})
        if 'preserve_background' not in stored:
            row['preserve_background'] = row.get('channels') in (2, 6)
        row['decision'] = playback_decision(row)
        suggestion = row.pop('dialogue_suggestion', None)
        row['suggested_text'] = suggestion['text'] if suggestion and suggestion['script'] == script_signature(row) else ''
        row['spoken_text'] = spoken_text(row['text'], data.get('pronunciation', DEFAULT_PRONUNCIATION))
        return row

    def save_pronunciation(self, identifier, rules, revision):
        normalized = [{'term': item['term'].strip(), 'spoken': item['spoken'].strip()} for item in rules]
        if any(not item['term'] or not item['spoken'] for item in normalized):
            raise ValueError('每条发音规则都需要关键词和读法')
        if len({item['term'].lower() for item in normalized}) != len(normalized):
            raise ValueError('发音规则中的关键词不能重复')
        with self.pipeline.lock, self.lock:
            state = self.editable(identifier, revision)
            for row in state['rows'].values():
                candidate = row.get('candidates', {}).get(row.get('selected'))
                if candidate:
                    config = candidate['config']
                    previous = config.get('spoken_text', config['text'])
                    if spoken_text(config['text'], normalized) != previous:
                        row.update(selected='', tested=False)
            state.update(pronunciation=normalized, revision=state['revision'] + 1, updated=now())
            self.write(identifier, state)
        return self.detail(identifier)

    def save_role_voices(self, identifier, role_voices, revision):
        if not isinstance(role_voices, dict) or len(role_voices) > 200:
            raise ValueError('角色音色映射无效')
        with self.pipeline.lock, self.lock:
            state = self.editable(identifier, revision)
            known = {row.get('role') for row in state['rows'].values() if row.get('role')}
            normalized = {}
            for role, voice_id in role_voices.items():
                role = str(role).strip()
                if not role or len(role) > 80:
                    raise ValueError('角色名称无效')
                if role not in known:
                    raise ValueError(f'未知角色：{role}')
                voice_id = str(voice_id or '').strip()
                if not voice_id:
                    continue
                if voice_id != state.get('role_voices', {}).get(role):
                    self.voice(voice_id)
                normalized[role] = voice_id
            state['role_voices'] = normalized
            state['revision'] += 1
            state['updated'] = now()
            self.write(identifier, state)
        return self.detail(identifier)

    def set_voice_conversion(self, identifier, enabled, revision):
        with self.pipeline.lock, self.lock:
            state = self.editable(identifier, revision)
            state['vc'] = bool(enabled)
            state['revision'] += 1
            state['updated'] = now()
            self.write(identifier, state)
        return self.detail(identifier)

    def rows(self, identifier, query, group, role, scene, status, offset, limit):
        data = self.state(identifier)
        entries = self.entries(identifier)
        query = query.casefold().strip()
        skipped_ids = set((data.get('job') or {}).get('skipped_rows') or [])
        results = []
        for row_id, entry in entries.items():
            if group and entry['group'] != group:
                continue
            row = self.row(identifier, row_id, data)
            if role and row['role'] != role or scene and row['scene'] != scene:
                continue
            if status == 'replaceable' and not row['replaceable']:
                continue
            if status == 'tested' and (row['decision'] != 'chinese' or not row['tested']):
                continue
            if status == 'ready' and row['decision'] != 'chinese' or status == 'unvoiced' and (row['candidates'] or row['keep_original']):
                continue
            if status in {'original', 'pending'} and row['decision'] != status:
                continue
            if status == 'no-script' and (row['ja'] or row['en']):
                continue
            if status == 'no-translation' and (row['text'] or not (row['ja'] or row['en'])):
                continue
            if status == 'risky' and not (RISKY_FIT & set((row.get('tts_fit') or '').split(','))):
                continue
            if status == 'no-text' and row['text'].strip():
                continue
            if status == 'has-suggestion' and not row['suggested_text']:
                continue
            if status == 'skipped' and row_id not in skipped_ids:
                continue
            current = row['candidates'].get(row['selected']) or next(reversed(row['candidates'].values()), {})
            if status == 'timing' and (current.get('delta') or 0) <= .15:
                continue
            if query and not any(query in str(value).casefold() for value in (row_id, row['ja'], row['en'], row['text'],
                        row['role'], row['scene'], entry['group'], *entry['events'])):
                continue
            results.append(dict(**{key: row[key] for key in ('id', 'group', 'role', 'scene', 'scene_order', 'ja', 'en', 'text',
                'text_source', 'channels', 'preserve_background', 'replaceable', 'selected', 'tested', 'decision',
                'source_duration', 'tts_fit') if key in row}, generated=bool(row['candidates']),
                duration=current.get('duration'), delta=current.get('delta'), window_delta=current.get('window_delta')))
        if scene:
            results.sort(key=lambda row: (row['scene_order'] == 0, row['scene_order']))
        return dict(rows=results[offset:offset + limit], total=len(results), revision=data['revision'])

    def fit_row_ids(self, identifier, query, group, role, scene, status, limit=0):
        """IDs matching the current table filter, used to queue a fit analysis."""
        result = self.rows(identifier, query, group, role, scene, status, 0, limit or 10 ** 9)
        return [str(row['id']) for row in result['rows']]

    @staticmethod
    def key(identifier):
        return 'game-voice:' + project_id(identifier)

    def editable(self, identifier, revision):
        if self.pipeline.busy(self.key(identifier)):
            raise HTTPException(409, '项目正在处理，请完成或取消后再编辑')
        data = self.state(identifier)
        if data['revision'] != revision:
            raise HTTPException(409, '项目已有新版本，请重新载入台词')
        return data

    def save_rows(self, identifier, updates):
        with self.lock:
            data = self.state(identifier)
            entries = self.entries(identifier)
            for row_id, patch in updates.items():
                existing = data['rows'].get(row_id, {})
                defaults = {**DEFAULT_ROW, 'preserve_background': entries[row_id].get('channels') in (2, 6)}
                selected = existing.get('candidates', {}).get(existing.get('selected'), {})
                synthesis = selected.get('synthesis', {})
                fields = VOICE_FIELDS
                if synthesis.get('method') in DRAMA_ROUTES:
                    fields = (*fields, *DRAMA_FIELDS)
                    if synthesis.get('source_language') and not existing.get('drama3_reference_text', '').strip():
                        fields = (*fields, synthesis['source_language'])
                if any(key in patch and patch[key] != existing.get(key, defaults[key]) for key in fields):
                    existing = {**existing, 'selected': '', 'tested': False}
                data['rows'][row_id] = {**existing, **patch}
            data['revision'] += 1
            data['updated'] = now()
            self.write(identifier, data)
            return data['revision']

    def set_original(self, identifier, row_ids, original, revision, *, protect_adopted=False):
        with self.pipeline.lock:
            data = self.editable(identifier, revision)
            updates = {}
            for row_id in dict.fromkeys(row_ids):
                row = self.row(identifier, row_id, data)
                if original and protect_adopted and row['decision'] == 'chinese':
                    continue
                if row['keep_original'] != original:
                    updates[row_id] = {'keep_original': original}
            if updates:
                self.save_rows(identifier, updates)
        return dict(project=self.detail(identifier), changed=len(updates))

    def package_plan(self, identifier, rows, release):
        cache = self.folder(identifier) / 'cache'
        plan = []
        for row in rows:
            choice = row['decision']
            candidate = row['candidates'].get(row['selected'], {})
            if choice == 'chinese':
                if not row['replaceable']:
                    choice = 'unsupported'
                elif not candidate or not (cache / (candidate.get('package_file') or candidate['file'])).is_file():
                    choice = 'missing'
                elif release and (candidate.get('window_delta') or 0) > .15:
                    choice = 'overrun'
                elif release and not row['tested']:
                    choice = 'untested'
                else:
                    choice = 'replace'
            plan.append(dict(id=row['id'], role=row['role'], scene=row['scene'], events=row['events'], status=choice,
                             text=row['text'] or row['ja'] or row['en'], delta=candidate.get('window_delta')))
        return plan

    def upload_guide(self, identifier, row_id, content, name, revision, settings):
        if not content or len(content) > MAX_GUIDE_BYTES:
            raise ValueError('中文引导音必须小于 32 MB')
        with self.pipeline.lock:
            self.row(identifier, row_id, self.editable(identifier, revision))
        folder = self.folder(identifier)
        cache = folder / 'cache'
        with TemporaryDirectory(prefix='guide-', dir=cache) as temporary:
            source = Path(temporary) / 'upload'
            normalized = source.with_suffix('.wav')
            source.write_bytes(content)
            extract_audio(source, normalized, settings, folder / 'process.log', Event(), duration=61, sample_rate=24000)
            length = wav_duration(normalized)
            if not .2 <= length <= 60:
                raise ValueError('中文引导音长度应为 0.2 至 60 秒')
            target = cache / f'guide-{file_hash(normalized)}.wav'
            with self.pipeline.lock:
                row = self.row(identifier, row_id, self.editable(identifier, revision))
                if not target.exists():
                    os.replace(normalized, target)
                if (row.get('guide') or {}).get('file') != target.name:
                    self.save_rows(identifier, {row_id: {'guide': dict(file=target.name, name=Path(name).name[:120],
                        duration=round(length, 3)), 'selected': '', 'tested': False}})

    def guide(self, identifier, row_id):
        file = (self.row(identifier, row_id).get('guide') or {}).get('file')
        if not file:
            raise FileNotFoundError(row_id)
        return safe_file(self.folder(identifier) / 'cache', file)

    def source(self, identifier, row_id, language, settings, cancel):
        data = self.state(identifier)
        asset = self.row(identifier, row_id, data)['assets'].get(language)
        if not asset:
            raise FileNotFoundError(language)
        sound = Path(data['sound'])
        original = safe_file(sound, asset['path'])
        stat = original.stat()
        base = self.folder(identifier) / 'cache'
        stamp = fingerprint([asset, stat.st_size, stat.st_mtime_ns])
        target = base / f'source-{stamp}.wav'
        if target.is_file():
            return target
        decoder, _, _ = tools()
        if not decoder:
            raise ValueError('原声解码需要 vgmstream-cli，请查看工作台的工具状态')
        log = self.folder(identifier) / 'process.log'
        with TemporaryDirectory(prefix='source-', dir=base) as temporary:
            scratch = Path(temporary)
            wem, wav = scratch / 'source.wem', scratch / 'decoded.wav'
            wem.write_bytes(extract_wem(sound, asset))
            run([decoder, '-i', '-o', wav, wem], log, cancel, timeout=45)
            extract_audio(wav, scratch / 'mono.wav', settings, log, cancel, sample_rate=24000)
            check_cancel(cancel)
            with self.lock:
                if not target.exists():
                    os.replace(scratch / 'mono.wav', target)
        return target

    def full_source(self, identifier, row_id, language, settings, cancel):
        """Decode the original WEM at its native channels and sample rate."""
        data = self.state(identifier)
        asset = self.row(identifier, row_id, data)['assets'].get(language)
        if not asset:
            raise FileNotFoundError(language)
        sound = Path(data['sound'])
        original = safe_file(sound, asset['path'])
        stat = original.stat()
        base = self.folder(identifier) / 'cache'
        stamp = fingerprint([asset, stat.st_size, stat.st_mtime_ns, 'full'])
        target = base / f'source-full-{stamp}.wav'
        if target.is_file():
            return target
        decoder, _, _ = tools()
        if not decoder:
            raise ValueError('原声解码需要 vgmstream-cli，请查看工作台的工具状态')
        log = self.folder(identifier) / 'process.log'
        with TemporaryDirectory(prefix='source-full-', dir=base) as temporary:
            scratch = Path(temporary)
            wem, wav = scratch / 'source.wem', scratch / 'decoded.wav'
            wem.write_bytes(extract_wem(sound, asset))
            run([decoder, '-i', '-o', wav, wem], log, cancel, timeout=45)
            check_cancel(cancel)
            with wave.open(str(wav), 'rb') as stream:
                if stream.getsampwidth() != 2 or stream.getnchannels() not in (1, 2, 4, 6):
                    raise ValueError('原声的采样格式或声道布局暂不支持')
            with self.lock:
                if not target.exists():
                    os.replace(wav, target)
        return target

    def background(self, identifier, source, settings, cancel, backend='demucs'):
        """Return the estimated background of a mono or stereo asset.

        5.1 assets are handled by replacing the centre channel directly; they
        do not need a separation model. Stereo assets are separated locally so
        their music, ambience and effects can be mixed under the new voice.
        """
        with wave.open(str(source), 'rb') as stream:
            channels = stream.getnchannels()
        if channels == 6:
            return source
        if channels not in (1, 2):
            raise ValueError('背景分离目前支持单声道、双声道或 5.1 语音')
        options = dict(backend=backend)
        if backend == 'bandit':
            if not bandit_available():
                raise ValueError('请先准备 BandIt v2 推理代码和官方多语种权重')
            model = BANDIT_CHECKPOINT
            weights = BANDIT_SHA256
            options.update(runner=str(BANDIT_SOURCE), weights_sha256=weights)
        else:
            model = resolve(settings.separation_model)
            if not separation_python().is_file() or not all((model / name).is_file()
                    for name in ('manifest.json', 'htdemucs.safetensors', 'config.json')):
                raise ValueError('请先准备 HTDemucs 背景分离环境')
            manifest = json.loads((model / 'manifest.json').read_text(encoding='utf-8'))
            weights = manifest.get('weights_sha256', '')
        stat = source.stat()
        stamp = fingerprint([source.name, stat.st_size, stat.st_mtime_ns, backend, weights])
        folder = self.folder(identifier)
        cache = folder / 'cache'
        output = cache / f'background-{stamp}.flac'
        if output.is_file():
            return output
        request = cache / f'background-{stamp}-request.json'
        progress = cache / f'background-{stamp}-progress.json'
        atomic_json(request, dict(model=str(model), source=str(source), output=str(output),
                                  duration=wav_duration(source), ffmpeg=settings.ffmpeg, progress=str(progress), **options))
        self.pipeline.drama3.release()
        self.pipeline.tts.release('为背景分离释放显存')
        run([separation_python(), ROOT / 'scripts' / 'separate_worker.py', request],
            folder / 'process.log', cancel, env=offline_env(), timeout=3600)
        check_cancel(cancel)
        if not output.is_file():
            raise ValueError('背景分离没有生成可用音频')
        return output

    def voice_source_rows(self, identifier, role, language, limit=150):
        """Rank a role's original lines by embedded WEM size (a proxy for length)."""
        data = self.state(identifier)
        entries = self.entries(identifier)
        choices = []
        for row_id, stored in data['rows'].items():
            if stored.get('role') != role or is_sound_note(stored):
                continue
            asset = (entries.get(row_id) or {}).get('assets', {}).get(language) or {}
            if not asset:
                continue
            # Longer lines make far better cloning references, and the catalogue
            # exposes the embedded WEM size even before a line has been decoded.
            choices.append((asset.get('size') or 0, row_id))
        choices.sort(key=lambda item: (-item[0], item[1]))
        return [row_id for _, row_id in choices[:limit]]

    def _reference_piece(self, identifier, row_id, language, settings, cancel, scratch, index, separate=True,
                         backend='demucs', separate_mono=False):
        """Decode one original line into a clean mono WAV for a voice reference."""
        log = self.folder(identifier) / 'process.log'
        try:
            full = self.full_source(identifier, row_id, language, settings, cancel)
        except ValueError:
            full = None
        target = scratch / f'piece-{index}.wav'
        if full is not None:
            with wave.open(str(full), 'rb') as stream:
                channels, width = stream.getnchannels(), stream.getsampwidth()
            if channels == 6 and width == 2:
                # Dialogue lives in the centre channel of a 5.1 game asset.
                with wave.open(str(full), 'rb') as src, wave.open(str(target), 'wb') as dst:
                    dst.setnchannels(1)
                    dst.setsampwidth(width)
                    dst.setframerate(src.getframerate())
                    samples = array('h', src.readframes(src.getnframes()))
                    if sys.byteorder != 'little':
                        samples.byteswap()
                    center = samples[2::6]
                    if sys.byteorder != 'little':
                        center.byteswap()
                    dst.writeframes(center.tobytes())
                return target
            if (channels == 2 or channels == 1 and separate_mono) and width == 2 and separate:
                try:
                    bed = self.background(identifier, full, settings, cancel, backend)
                    mono = '[0:a]pan=mono|c0=0.5*c0+0.5*c1' if channels == 2 else '[0:a]anull'
                    ffmpeg(settings, ["-i", str(full), "-i", str(bed),
                        "-filter_complex", f"{mono}[source];[1:a]pan=mono|c0=0.5*c0+0.5*c1,volume=-1[inverse];[source][inverse]amix=inputs=2:duration=first:normalize=0[voice]",
                        "-map", "[voice]", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target)], log, cancel)
                    return target
                except ValueError:
                    pass
        return self.source(identifier, row_id, language, settings, cancel)

    def emotion_reference(self, identifier, row, language, settings, cancel):
        """Extract the original line as an emotion reference (fast, cached per line)."""
        asset = row['assets'].get(language)
        if not asset:
            return None
        folder = self.folder(identifier)
        cache = folder / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        log = folder / 'process.log'
        preserve = bool(row.get('preserve_background'))
        if not preserve and 'mixed' in row.get('tts_fit', '').split(','):
            return None
        backend = row.get('background_backend', 'demucs')
        stamp = fingerprint([asset, 'emotion', 'v3', preserve, backend])
        target = cache / f'emotion-{stamp}.wav'
        if target.is_file() and wav_duration(target) >= .35:
            return target
        with TemporaryDirectory(prefix='emotion-', dir=cache) as temporary:
            scratch = Path(temporary)
            piece = self._reference_piece(identifier, row['id'], language, settings, cancel, scratch, 'emotion',
                                          separate=preserve, backend=backend, separate_mono=preserve)
            normalized = scratch / 'emotion.wav'
            extract_audio(piece, normalized, settings, log, cancel, duration=15, sample_rate=24000)
            if wav_duration(normalized) < .35:
                return None
            with self.lock:
                if not target.exists():
                    os.replace(normalized, target)
        return target

    def extract_role_voice(self, identifier, rows, role, name, language, settings, cancel, *, selected=False):
        """Build a role reference. Explicit selections are saved for audition."""
        folder = self.folder(identifier)
        cache = folder / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        log = folder / 'process.log'
        with TemporaryDirectory(prefix='voice-ref-', dir=cache) as temporary:
            scratch = Path(temporary)
            pieces = []
            used = []
            duration = 0.0
            for index, row in enumerate(rows):
                check_cancel(cancel)
                self.update_job(identifier, phase='正在提取角色原声')
                piece = self._reference_piece(identifier, row['id'], language, settings, cancel, scratch, index)
                normalized = scratch / f'norm-{index}.wav'
                extract_audio(piece, normalized, settings, log, cancel, sample_rate=24000)
                length = wav_duration(normalized)
                if length < .6:
                    continue
                pieces.append(normalized)
                used.append(row['id'])
                duration += length
                if selected:
                    if duration + (len(pieces) - 1) * .25 >= 30:
                        break
                    continue
                # One continuous 3–10 s utterance clones best; only concatenate a
                # few longest lines when no single line is long enough.
                if len(pieces) == 1 and 3 <= length <= 10:
                    break
                if duration >= 6 or (len(pieces) >= 4 and duration >= 3):
                    break
                if len(pieces) >= 12:
                    break
            if not pieces:
                raise ValueError('没有可用的原声台词，请先确认该角色有可解码的原声')
            silence = scratch / 'silence.wav'
            with wave.open(str(silence), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(b'\x00\x00' * 6000)
            ordered = []
            for index, piece in enumerate(pieces):
                if index:
                    ordered.append(silence)
                ordered.append(piece)
            listing = scratch / 'list.txt'
            listing.write_text('\n'.join(f"file '{path.as_posix()}'" for path in ordered), encoding='utf-8')
            target = scratch / 'reference.wav'
            ffmpeg(settings, ['-f', 'concat', '-safe', '0', '-i', str(listing),
                '-t', '30' if selected else '10', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(target)], log, cancel)
            check_cancel(cancel)
            total = wav_duration(target)
            if total < 3:
                raise ValueError(f'该角色的原声合计只有 {total:.1f} 秒，不足 3 秒；请手动上传参考音')
            self.update_job(identifier, phase='正在保存角色音色')
            title = self.state(identifier)['title']
            if selected:
                name = (name or '').strip() or f"{role[:75]}（选段 {used[0]}）"
                reference_rows = '、'.join(f'#{row_id}' for row_id in used)
                return self.voices.save(name, f'从 {title} 的 {role} 原声台词提取：{reference_rows}', target)
            name = (name or '').strip() or f'{role}（游戏原声）'
            # Re-extracting a role should refresh its preset instead of piling up copies.
            existing = next((item for item in self.voices.list() if item['name'] == name), None)
            voice = self.voices.save(name, f'从 {title} 的原声提取，角色 {role}', target,
                                     identifier=existing['id'] if existing else None)
            with self.lock:
                data = self.state(identifier)
                data.setdefault('role_voices', {})[role] = voice['id']
                data['revision'] += 1
                data['updated'] = now()
                self.write(identifier, data)
            return voice

    def candidate(self, identifier, row_id, candidate, track='preview'):
        value = self.row(identifier, row_id)['candidates'].get(candidate)
        if value is None:
            raise FileNotFoundError(candidate)
        file = value['file']
        if track == 'mix':
            file = value.get('package_file') or file
        elif track == 'dry':
            file = value.get('dry_file') or (file if not value.get('package_file') else '')
        elif track == 'performance':
            file = value.get('performance_file') or ''
        elif track == 'background':
            file = value.get('background_file') or ''
        if not file:
            raise FileNotFoundError(track)
        return safe_file(self.folder(identifier) / 'cache', file)

    def update_job(self, identifier, **values):
        with self.lock:
            data = self.state(identifier)
            if data['job'].get('status') == 'cancelling' and values.get('status', 'running') in {'running', 'cancelling'}:
                values.pop('status', None)
                values.pop('phase', None)
            data['job'].update(values)
            self.write(identifier, data)

    def mark_done(self, identifier, ids, **values):
        with self.lock:
            job = self.state(identifier)['job']
            done_rows = list(dict.fromkeys([*job['done_rows'], *ids]))
            self.update_job(identifier, done_rows=done_rows, done=len(done_rows), **values)

    def submit(self, identifier, action, row_ids, settings, revision, *, language='ja', voice='', release=False,
               variant='', role='', name='', emotion='', force_batch_settings=False, route='project', vc=None):
        key = self.key(identifier)
        with self.pipeline.lock:
            data = self.editable(identifier, revision)
            role_voices = data.get('role_voices', {})
            if route == 'warm':
                route = 'drama3' if action in {'generate', 'alternate'} and self.pipeline.drama3.warm(settings) else 'project'
            if vc is None:
                vc = route == 'conversion' or route == 'project' and bool(data.get('vc')) and settings.vc_provider != 'off'
            if route in DRAMA_ROUTES:
                vc = route == 'drama3_conversion'
            entries = self.entries(identifier)
            if not row_ids or len(set(row_ids)) != len(row_ids) or any(row_id not in entries for row_id in row_ids):
                raise ValueError('请选择有效的台词')
            if action == 'voice_from_source' and not role:
                raise ValueError('请选择要提取音色的角色')
            if emotion and emotion not in PRESETS:
                raise ValueError('无效情绪预设')
            rows = [self.row(identifier, row_id, data) for row_id in row_ids]
            if action == 'voice_from_selection':
                roles = {row['role'] for row in rows}
                if len(roles) != 1 or roles & {'', '-', '???'}:
                    raise ValueError('请勾选同一角色的原声台词')
                role = roles.pop()
                language = data['language']
                if any(language not in row['assets'] or is_sound_note(row) for row in rows):
                    raise ValueError('所选台词含音效占位或缺少项目语言原声')
            if action == 'transcribe_translate':
                rows = [row for row in rows if not row['text'] or not (row['ja'] or row['en'])]
                if not rows:
                    raise ValueError('所选台词已有原文和中文配音稿')
            asr_rows = rows if action == 'asr' else [row for row in rows if not (row['ja'] or row['en'])]
            if action in {'asr', 'transcribe_translate'} and any(language not in row['assets'] for row in asr_rows):
                raise ValueError('部分台词缺少所选语言的原声')
            if action in {'asr', 'transcribe_translate'} and asr_rows:
                from .project_experience import validate_language
                validate_language(settings, language)
            if action == 'translate':
                rows = [row for row in rows if not row['text'] and (row['ja'] or row['en'])]
                if not rows:
                    raise ValueError('请选择已有原文且缺少中文配音稿的台词')
            if action == 'polish':
                rows = [row for row in rows if row['text'].strip() and (row['ja'].strip() or row['en'].strip())]
                if not rows:
                    raise ValueError('请为要口语化的台词填写原文和中文配音稿')
            skipped = []
            if action in {'generate', 'alternate'}:
                if vc and settings.vc_provider == 'off':
                    raise ValueError('请先在模型设置中启用 Seed-VC')
                if action == 'alternate' and not variant:
                    variant = uuid.uuid4().hex
                spoken_rows = []
                for row in rows:
                    if any(character.isalnum() for character in row['spoken_text']):
                        spoken_rows.append(row)
                    else:
                        skipped.append(row['id'])
                rows = spoken_rows
                if not rows:
                    raise ValueError('所选台词都没有可朗读内容，请先填写配音稿')
                if vc:
                    for role in sorted({row['role'] for row in rows}):
                        self.conversion_target(role, role_voices)
                effective = [{**row, **generation_settings(row, voice, role_voices, emotion, force_batch_settings, vc)}
                             for row in rows if route in DRAMA_ROUTES or not (vc and row.get('guide'))]
                if route in DRAMA_ROUTES:
                    problem = drama3.status(settings)
                    if problem:
                        raise ValueError(problem)
                    if vc:
                        voices = {item['voice'] for item in effective}
                        if '' in voices or voices.intersection(role_voices.values()):
                            raise ValueError('请选择普通话参考音，角色绑定音色专用于变声')
                    else:
                        voices = {item['voice'] for item in effective if item['voice'] and item['emotion'] != 'source'}
                    for voice_id in voices:
                        self.voice(voice_id, usage='普通话表演参考音色' if vc else '合成参考音色')
                elif effective:
                    validate_tts(settings, 'zh', effective)
                    if tts_backend(settings).reference_audio:
                        for voice_id in {item['voice'] for item in effective}:
                            self.voice(voice_id)
            if action == 'package':
                plan = self.package_plan(identifier, rows, release)
                blocked = [entry['id'] for entry in plan if entry['status'] not in {'replace', 'original'}]
                if blocked:
                    raise ValueError('以下台词需要处理后才能打包：' + '、'.join(blocked[:8]))
                rows = [row for row in rows if row['decision'] == 'chinese']
                if not rows:
                    raise ValueError('所选台词均保留原版，没有需要打包的中文语音')
                if not all(tools()[1:]):
                    raise ValueError('打包需要 Wwise 2014 CLI 与语音转换模板')
            cancel = Event()
            options = dict(language=language, voice=voice, release=release, variant=variant, role=role, name=name,
                           emotion=emotion, force_batch_settings=force_batch_settings, route=route, vc=vc)
            self.update_job(identifier, kind=action, status='queued', phase='排队中', error='', done=0, total=len(rows),
                            voice_result=None,
                            row_ids=[row['id'] for row in rows], done_rows=[], skipped_rows=skipped, options=options)
            self.pipeline.events[key] = cancel
            try:
                self.pipeline.futures[key] = self.pipeline.pool.submit(
                    self.execute, identifier, action, rows, settings, cancel, **options)
            except RuntimeError:
                self.pipeline.events.pop(key, None)
                self.update_job(identifier, status='failed', phase='任务未能加入处理队列')
                raise
            return self.detail(identifier)

    def retry(self, identifier, settings, revision):
        with self.pipeline.lock:
            job = self.editable(identifier, revision)['job']
            if job.get('status') not in {'failed', 'interrupted', 'cancelled'}:
                raise ValueError('只有失败、中断或取消的任务可以重试')
            completed = set(job.get('done_rows', []))
            row_ids = [row_id for row_id in job.get('row_ids', []) if row_id not in completed]
            if not row_ids:
                raise ValueError('上次任务没有未完成的台词')
            return self.submit(identifier, job['kind'], row_ids, settings, revision, **job.get('options', {}))

    def cancel(self, identifier):
        self.state(identifier)
        key = self.key(identifier)
        with self.pipeline.lock:
            event = self.pipeline.events.get(key)
            if event:
                event.set()
                if self.pipeline.futures[key].cancel():
                    self.pipeline.events.pop(key)
                    self.pipeline.futures.pop(key)
                    self.update_job(identifier, status='cancelled', phase='任务已取消')
                else:
                    self.update_job(identifier, status='cancelling', phase='正在取消')
        return self.detail(identifier)

    def execute(self, identifier, action, rows, settings, cancel, language, voice, release, variant,
                role='', name='', emotion='', force_batch_settings=False, route='project', vc=False):
        status, phase, error = 'done', '任务完成', ''
        drama_job = action in {'generate', 'alternate'} and route in DRAMA_ROUTES
        try:
            self.update_job(identifier, status='running', phase='正在准备')
            if not drama_job and action in {'asr', 'transcribe_translate', 'translate', 'polish', 'generate', 'alternate'}:
                self.pipeline.drama3.release()
            if action == 'asr':
                self.recognize(identifier, rows, language, settings, cancel)
            elif action == 'transcribe_translate':
                self.transcribe_translate(identifier, rows, language, settings, cancel)
            elif action == 'voice_from_source':
                self.extract_role_voice(identifier, rows, role, name, language, settings, cancel)
            elif action == 'voice_from_selection':
                reference = self.extract_role_voice(identifier, rows, role, name, language, settings, cancel, selected=True)
                self.update_job(identifier, voice_result={'id': reference['id'], 'name': reference['name']})
            elif action == 'analyze_fit':
                self.analyze_fit(identifier, rows, settings, cancel)
            elif action in {'translate', 'polish'}:
                self.translate(identifier, rows, settings, cancel, polish=action == 'polish')
            elif action in {'generate', 'alternate'}:
                if drama_job:
                    self.generate_drama3(identifier, rows, voice, settings, cancel, variant, emotion,
                                         force_batch_settings, vc)
                else:
                    self.generate(identifier, rows, voice, settings, cancel, variant, emotion, force_batch_settings, vc)
            else:
                self.package(identifier, rows, release, settings, cancel)
            check_cancel(cancel)
        except Cancelled:
            status, phase = 'cancelled', '任务已取消'
        except Exception as exc:
            status, phase, error = 'failed', '处理失败', str(exc) or type(exc).__name__
            with (self.folder(identifier) / 'process.log').open('a', encoding='utf-8') as log:
                log.write(f'{action}: {type(exc).__name__}: {exc}\n')
        finally:
            if drama_job:
                self.pipeline.drama3.retain(settings.drama3_idle_seconds if status == 'done' and not vc else 0)
            with self.pipeline.lock:
                key = self.key(identifier)
                try:
                    if status == 'done':
                        self.mark_done(identifier, [row['id'] for row in rows], status=status, phase=phase)
                    else:
                        self.update_job(identifier, status=status, phase=phase, error=error)
                finally:
                    self.pipeline.events.pop(key, None)
                    self.pipeline.futures.pop(key, None)

    def transcribe_translate(self, identifier, rows, language, settings, cancel):
        needs_script = [row for row in rows if not (row['ja'] or row['en'])]
        if needs_script:
            self.recognize(identifier, needs_script, language, settings, cancel, mark_complete=False)
        data = self.state(identifier)
        current = [self.row(identifier, row['id'], data) for row in rows]
        has_script = [row for row in current if row['ja'] or row['en']]
        self.mark_done(identifier, [row['id'] for row in has_script if row['text']])
        pending = [row for row in has_script if not row['text']]
        if pending:
            self.translate(identifier, pending, settings, cancel)
        missing = len(current) - len(has_script)
        if missing:
            raise ValueError(f'{missing} 条原声未识别出可用文字，可试听后手工录入或重试。')

    def recognize(self, identifier, rows, language, settings, cancel, *, mark_complete=True):
        folder = self.folder(identifier)
        self.pipeline.tts.release('为游戏语音识别释放显存')
        for row in rows:
            check_cancel(cancel)
            self.update_job(identifier, phase='正在识别原声')
            if row[language]:
                if mark_complete:
                    self.mark_done(identifier, [row['id']])
                continue
            source = self.source(identifier, row['id'], language, settings, cancel)
            with TemporaryDirectory(prefix='asr-', dir=folder / 'cache') as temp:
                path = Path(temp)
                log = folder / 'process.log'
                extract_audio(source, path / 'asr.wav', settings, log, cancel, sample_rate=16000)
                output = path / 'asr.json'
                transcribe(settings, path / 'asr.wav', output, language, 'anime', log, cancel, offline_env())
                content = ''.join(item['text'] for item in json.loads(output.read_text(encoding='utf-8'))).strip()
            if content:
                self.save_rows(identifier, {row['id']: {language: content, language + '_source': 'asr'}})
            if mark_complete:
                self.mark_done(identifier, [row['id']])

    def translate(self, identifier, rows, settings, cancel, *, polish=False):
        folder = self.folder(identifier)
        data = self.state(identifier)
        self.pipeline.tts.release('为游戏台词翻译释放显存')
        self.update_job(identifier, phase='正在口语化配音稿' if polish else '正在翻译台词')
        failed = []
        original = {int(row['id']): row for row in rows}
        with local_translator(settings, folder / 'process.log', cancel) as url:
            for language in ('ja', 'en'):
                batch = [dict(id=int(row['id']), start=index, end=index + 1, text=row[language], zh=row['text'],
                              speaker=row['role'], scene=row['scene'], source_duration=row.get('source_duration'))
                         for index, row in enumerate(rows) if row[language].strip() and (language == 'ja' or not row['ja'].strip())]
                topic = dict(title=data['title'], language=language, target_language='zh', mode='game')
                for offset in range(0, len(batch), 8):
                    check_cancel(cancel)
                    indices = list(range(offset, min(offset + 8, len(batch))))
                    current = [batch[i] for i in indices]
                    context = translation_context(batch, indices)
                    options = dict(cache=folder / 'cache', topic=topic,
                                   on_failure=lambda row, _error: failed.append(str(row['id'])))
                    if polish:
                        result = polish_translation(url, settings, current, context, 'game', cancel, **options)
                        updates = {}
                        for key, item in result.items():
                            row, text = original[key], item['zh']
                            if len(text) > 1500:
                                failed.append(str(key))
                                continue
                            suggestion = dict(text=text, script=script_signature(row)) if text != row['text'] else None
                            updates[str(key)] = {'dialogue_suggestion': suggestion}
                    else:
                        result = translate_batch(url, settings, current, context, 'game', cancel, **options)
                        updates = {str(key): {'text': text} for key, text in result.items()}
                    if updates:
                        self.save_rows(identifier, updates)
                        self.mark_done(identifier, list(updates))
        if failed:
            raise ValueError('部分译文需要人工处理，已保留成功结果：' + ', '.join(failed))

    def analyze_fit(self, identifier, rows, settings, cancel):
        """Score original audio for TTS suitability and store the flags per line."""
        data = self.state(identifier)
        language = data['language']
        self.update_job(identifier, phase='正在分析 TTS 适配性')
        pending, done = {}, []
        for index, row in enumerate(rows):
            check_cancel(cancel)
            try:
                source = self.source(identifier, row['id'], language, settings, cancel)
                flags = fit_flags(source) & RISKY_FIT
            except (OSError, ValueError, RuntimeError):
                flags = set()
            pending[row['id']] = {'tts_fit': ','.join(sorted(flags)) if flags else 'ok'}
            done.append(row['id'])
            if len(pending) >= 25 or index == len(rows) - 1:
                self.save_rows(identifier, dict(pending))
                self.mark_done(identifier, done)
                pending, done = {}, []

    def voice(self, voice_id, *, usage='参考音色', repair='请重新选择'):
        if not voice_id:
            raise ValueError('请为台词选择中文音色，或指定本批默认音色')
        try:
            voice = self.voices.read(voice_id)
        except FileNotFoundError as exc:
            raise ValueError(f'{usage}的记录不存在。{repair}') from exc
        if voice.get('archived'):
            raise ValueError(f'{usage}「{voice["name"]}」已归档。{repair}')
        return voice

    def conversion_target(self, role, role_voices):
        target = role_voices.get(role)
        if not target:
            raise ValueError(f'请先在「角色音色」为「{role or "未分配角色"}」绑定变声目标音色')
        return self.voice(target, usage=f'角色「{role}」绑定的变声音色', repair='请在「角色音色」重新绑定')

    def generate(self, identifier, rows, default_voice, settings, cancel, variant='', emotion='', force_batch_settings=False, vc=False):
        folder = self.folder(identifier)
        cache = folder / 'cache'
        log = folder / 'process.log'
        signature = tts_model_signature(settings)
        data = self.state(identifier)
        language = data['language']
        role_voices = data.get('role_voices', {})

        choices = {row['id']: generation_settings(row, default_voice, role_voices, emotion, force_batch_settings, vc)
                   for row in rows}
        emotion_refs = {}
        for row in rows:
            if choices[row['id']]['emotion'] == 'source' and not (vc and row.get('guide')):
                check_cancel(cancel)
                self.update_job(identifier, phase='正在提取原声情绪')
                reference = self.emotion_reference(identifier, row, language, settings, cancel)
                if reference is not None:
                    emotion_refs[row['id']] = reference
        plans, pending, timed_clips = [], {}, {}
        with self.voices.lock:
            for row in rows:
                check_cancel(cancel)
                if vc and row.get('guide'):
                    guide = self.guide(identifier, row['id'])
                    plans.append((row, guide, row['guide']['name'], dict(method='guide', emotion='inherit')))
                    continue
                choice = choices[row['id']]
                voice_id, preset_emotion = choice['voice'], choice['emotion']
                reference = digest = ''
                voice_name = settings.tts_voice
                if tts_backend(settings).reference_audio:
                    preset = self.voice(voice_id)
                    source = self.voices.folder(voice_id) / preset['audio']
                    digest = file_hash(source)
                    reference = f'cache/ref-{digest}.wav'
                    if not (folder / reference).exists():
                        shutil.copyfile(source, folder / reference)
                    voice_name = preset['name']
                emotion_reference = emotion_refs.get(row['id'])
                clip = make_voice_clip({**row, 'zh': row['spoken_text'], 'speaker': voice_id,
                                        'emotion': preset_emotion}, folder, reference, settings, signature, digest,
                                       emotion_reference=emotion_reference)
                actual_emotion = 'inherit' if preset_emotion == 'source' and emotion_reference is None else preset_emotion
                synthesis = dict(method='conversion' if vc else 'direct', voice_id=voice_id if reference else '', emotion=actual_emotion,
                                 emotion_requested=preset_emotion,
                                 emotion_strength=clip.get('emotion_strength') if actual_emotion != 'inherit' else None)
                raw = Path(clip['output'])
                if variant:
                    raw = cache / f'tts-{fingerprint([raw.name, variant])}.wav'
                    clip['output'] = str(raw)
                if not reusable_voice_clip(raw):
                    pending.setdefault(raw, clip)
                if settings.tts_provider == 'indextts' and settings.index_version == '2.5':
                    timed_clips[row['id']] = clip
                plans.append((row, raw, voice_name, synthesis))

        def report(_identifier, _stage, _percent, message, detail=None):
            self.update_job(identifier, phase=message)

        adapter_store = SimpleNamespace(folder=lambda _identifier: folder, update_runtime=lambda *_args, **_kwargs: None,
                                        log=lambda _identifier, _message: None)
        adapter = SimpleNamespace(store=adapter_store, tts=self.pipeline.tts, progress=report)
        errors = []
        try:
            Pipeline.synthesize(adapter, identifier, list(pending.values()), len(plans), settings, cancel, 'audition')
        except Cancelled:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))
        timing = {}
        retry_clips = {}
        for row, raw, _voice_name, _synthesis in plans:
            clip = timed_clips.get(row['id'])
            if clip is None or not reusable_voice_clip(raw):
                continue
            length = wav_duration(raw)
            trims = row['trim_start'] + row['trim_end']
            padding = row['lead'] + row['tail']
            target_duration = row['target_duration'] or row.get('source_duration')
            if not target_duration:
                try:
                    target_duration = wav_duration(self.source(identifier, row['id'], language, settings, cancel))
                except Cancelled:
                    raise
                except (OSError, RuntimeError, ValueError):
                    continue
            budget = max(.08, (target_duration - padding) * row['speed'] + trims)
            if length - budget > .08:
                factor = round(max(.85, budget / length), 3)
                output = cache / f'tts-{fingerprint([raw.name, "duration_factor", factor])}.wav'
                timing[row['id']] = output, budget, factor
                if not reusable_voice_clip(output):
                    retry_clips.setdefault(output, {**clip, 'output': str(output), 'duration_factor': factor})
        if retry_clips:
            try:
                Pipeline.synthesize(adapter, identifier, list(retry_clips.values()), len(timing), settings, cancel, 'audition')
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(str(exc))
        for index, (row, raw, voice_name, synthesis) in enumerate(plans):
            if row['id'] in timing:
                output, budget, factor = timing[row['id']]
                if reusable_voice_clip(output) and abs(wav_duration(output) - budget) < abs(wav_duration(raw) - budget):
                    plans[index] = (row, output, voice_name, {**synthesis, 'duration_factor': factor})
        converted = self.convert_candidate_voices(identifier, plans, role_voices, settings, cancel) if vc else {}
        for row, performance, voice_name, synthesis in plans:
            check_cancel(cancel)
            raw = converted.get(row['id'], performance)
            if vc:
                synthesis = {**synthesis, 'vc_target': role_voices.get(row.get('role') or '')}
            try:
                self.save_candidate(identifier, row, raw, voice_name, synthesis, language, settings, cancel,
                                    performance.name if vc else '')
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{row['id']}: {exc}")
        if errors and self.state(identifier)['job']['done'] != len(rows):
            raise ValueError('\n'.join(errors[:5]))

    def convert_candidate_voices(self, identifier, plans, role_voices, settings, cancel):
        folder = self.folder(identifier)
        cache, log = folder / 'cache', folder / 'process.log'
        clips, targets, converted = [], {}, {}
        for row, raw, _voice_name, _synthesis in plans:
            check_cancel(cancel)
            target = role_voices.get(row.get('role') or '')
            if not target or not reusable_voice_clip(raw):
                continue
            if target not in targets:
                voice = self.conversion_target(row['role'], role_voices)
                audio = self.voices.folder(target) / voice['audio']
                targets[target] = audio, file_hash(audio)
            audio, digest = targets[target]
            stamp = fingerprint([raw.name, digest, settings.vc_steps, settings.vc_cfg_rate])
            output = cache / f'vc-{stamp}.wav'
            converted[row['id']] = output
            if not reusable_voice_clip(output):
                clips.append(dict(id=row['id'], source=raw, target=audio, output=output))
        if clips:
            self.pipeline.tts.release('为变声释放显存')
            self.update_job(identifier, phase='正在变声到角色音色')
            voice_conversion.convert(
                settings, clips, cache / 'vc', log, cancel,
                on_progress=lambda done, total: self.update_job(identifier, phase=f'正在变声 {done}/{total}'),
                on_fallback=lambda: self.update_job(identifier, phase='显卡变声失败，正在用 CPU 继续'))
        return converted

    def drama3_reference_text(self, reference, digest, cache, settings, log, cancel):
        model = resolve(settings.asr_model) / 'model.bin'
        if not model.is_file():
            raise ValueError('请填写 Drama 3 参考音原文，或准备本地 faster-whisper 模型以自动识别')
        stamp = fingerprint([digest, str(model), model.stat().st_mtime_ns])
        transcript = cache / f'drama3-reference-{stamp}.txt'
        if transcript.is_file():
            return transcript.read_text(encoding='utf-8')
        if settings.asr_device == 'cuda':
            self.pipeline.drama3.release()
        with TemporaryDirectory(prefix='drama3-asr-', dir=cache) as temp:
            audio, result = Path(temp) / 'reference.wav', Path(temp) / 'asr.json'
            extract_audio(reference, audio, settings, log, cancel, sample_rate=16000)
            local = settings.model_copy(update={'asr_provider': 'faster_whisper'})
            transcribe(local, audio, result, '', 'anime', log, cancel, offline_env())
            text = ' '.join(item['text'].strip() for item in json.loads(result.read_text(encoding='utf-8'))).strip()
            if not any(character.isalnum() for character in text):
                raise ValueError('Drama 3 未识别到参考音中的台词，请填写参考音原文')
            check_cancel(cancel)
            pending = Path(temp) / 'transcript.txt'
            pending.write_text(text, encoding='utf-8')
            pending.replace(transcript)
            return text

    def generate_drama3(self, identifier, rows, default_voice, settings, cancel, variant, emotion, force_batch_settings, vc=False):
        folder = self.folder(identifier)
        cache, log = folder / 'cache', folder / 'process.log'
        data = self.state(identifier)
        role_voices = data.get('role_voices', {})
        problem = drama3.status(settings)
        if problem:
            raise ValueError(problem)
        signature = [(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in drama3.paths(settings)]
        self.pipeline.tts.release('为 Drama 3 释放显存')
        asr_model = resolve(settings.asr_model)
        speech_check = drama3.JapaneseOutputCheck(asr_model) if (asr_model / 'model.bin').is_file() else None
        errors, requests, plans = [], [], []
        for row in rows:
            check_cancel(cancel)
            try:
                choice = generation_settings(row, default_voice, role_voices, emotion, force_batch_settings, vc)
                voice_id, preset = choice['voice'], choice['emotion']
                if vc and preset == 'source':
                    preset = 'inherit'
                direction_override = row['drama3_direction'].strip()
                direction = direction_override or drama3.DIRECTOR_TAGS.get(preset, '')
                if vc:
                    direction = '自然、清晰的标准普通话' + (f'，{direction}' if direction else '')
                reference, transcript, digest, voice_name = None, '', '', '模型默认音色'
                source_language = data['language'] if preset == 'source' else ''
                if source_language:
                    self.update_job(identifier, phase='正在提取 Drama 3 原声参考')
                    reference = self.emotion_reference(identifier, row, source_language, settings, cancel)
                    if reference is None:
                        raise ValueError('本句原声无法用作 Drama 3 参考音，请选择角色参考音')
                    voice_id, digest, voice_name = '', file_hash(reference), '本句原声'
                elif voice_id:
                    with self.voices.lock:
                        voice = self.voice(voice_id)
                        source = self.voices.folder(voice_id) / voice['audio']
                        digest, voice_name = file_hash(source), voice['name']
                        reference = cache / f'ref-{digest}.wav'
                        if not reference.is_file():
                            with TemporaryDirectory(prefix='drama3-ref-', dir=cache) as temp:
                                copy = Path(temp) / 'reference.wav'
                                shutil.copyfile(source, copy)
                                copy.replace(reference)
                if reference:
                    transcript = row['drama3_reference_text'].strip()
                    if not transcript and source_language and wav_duration(reference) < 14.99:
                        transcript = row[source_language].strip()
                    if not transcript:
                        self.update_job(identifier, phase='正在识别 Drama 3 参考音')
                        transcript = self.drama3_reference_text(reference, digest, cache, settings, log, cancel)
                japanese_reference = bool(source_language == 'ja' or drama3.KANA.search(transcript))
                if vc and japanese_reference:
                    raise ValueError('Drama 3 普通话表演参考音包含日语，请选择普通话参考音')
                spoken = row['spoken_text']
                cross_language = bool(japanese_reference and re.search(r'[\u3400-\u9fff]', spoken) and not drama3.KANA.search(spoken))
                if cross_language:
                    direction = drama3.MANDARIN_DIRECTION + (f'，{direction}' if direction else '')
                text = f"[{direction}] {spoken}" if direction else spoken
                offload = settings.drama3_cpu_expert_layers if settings.drama3_backend != 'cpu' else 0
                key = [signature, settings.drama3_backend, offload, text, digest, transcript, variant]
                if cross_language and speech_check:
                    key.append('japanese-screen-v1')
                stamp = fingerprint(key)
                seed = int(stamp[:8], 16) & 0x7fffffff
                output = cache / f'drama3-{stamp}.wav'
                synthesis = dict(method='drama3_conversion' if vc else 'drama3',
                                 voice_id=voice_id, direction=direction, seed=seed,
                                 backend=settings.drama3_backend, reference=bool(reference),
                                 emotion=preset if source_language or not direction_override else 'inherit',
                                 source_language=source_language)
                requests.append((row, output, voice_name, synthesis, text, reference, transcript, cross_language))
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{row['id']}: {exc}")
        for index, (row, output, voice_name, synthesis, text, reference, transcript, cross_language) in enumerate(requests, 1):
            check_cancel(cancel)
            try:
                if not reusable_voice_clip(output):
                    self.update_job(identifier, phase=f'正在生成 Drama 3 配音 {index}/{len(requests)}')
                    if cross_language and speech_check:
                        with TemporaryDirectory(prefix='drama3-check-', dir=cache) as temp:
                            draft = Path(temp) / 'draft.wav'
                            self.pipeline.drama3.render(settings, text, reference, transcript, synthesis['seed'], draft, log, cancel)
                            self.update_job(identifier, phase='正在核对 Drama 3 配音语言')
                            if speech_check.reject(draft, settings, log, cancel):
                                raise ValueError('Drama 3 生成了日语，已拦截，请改用「Drama 3 普通话 + Seed-VC」')
                            draft.replace(output)
                    else:
                        self.pipeline.drama3.render(settings, text, reference, transcript, synthesis['seed'], output, log, cancel)
                if vc or row.get('preserve_background'):
                    plans.append((row, output, voice_name, synthesis))
                else:
                    self.save_candidate(identifier, row, output, voice_name, synthesis, data['language'], settings, cancel)
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{row['id']}: {exc}")
        converted = {}
        if vc and plans:
            self.pipeline.drama3.release()
            converted = self.convert_candidate_voices(identifier, plans, role_voices, settings, cancel)
        for row, performance, voice_name, synthesis in plans:
            try:
                output = performance
                if vc:
                    target = role_voices[row['role']]
                    synthesis['vc_target'] = target
                    voice_name = f"{voice_name} → {self.voice(target)['name']}"
                    output = converted[row['id']]
                self.save_candidate(identifier, row, output, voice_name, synthesis,
                                    data['language'], settings, cancel, performance.name if vc else '')
            except Cancelled:
                raise
            except (OSError, KeyError, RuntimeError, ValueError) as exc:
                errors.append(f"{row['id']}: {exc}")
        if errors:
            raise ValueError('\n'.join(errors[:5]))

    def save_candidate(self, identifier, row, raw, voice_name, synthesis, language, settings, cancel, performance=''):
        folder = self.folder(identifier)
        source = None
        if row.get('preserve_background'):
            self.update_job(identifier, phase='正在保留原声背景与音效')
            source = self.full_source(identifier, row['id'], language, settings, cancel)
        wav, stamp, package_wav, voice_duration = self.render_candidate(
            row, raw, settings, folder / 'cache', folder / 'process.log', cancel, source)
        original = self.source(identifier, row['id'], language, settings, cancel)
        duration, original_duration = wav_duration(wav), wav_duration(original)
        fields = (*VOICE_FIELDS, *DRAMA_FIELDS) if synthesis['method'] in DRAMA_ROUTES else VOICE_FIELDS
        candidate = dict(duration=round(duration, 3), voice_duration=round(voice_duration, 3),
            delta=round(voice_duration - original_duration, 3), file=wav.name,
            window_delta=round(voice_duration - (row['target_duration'] or original_duration), 3),
            package_file=package_wav.name if package_wav else '',
            dry_file=f'candidate-{stamp}-voice.wav' if package_wav else wav.name,
            background_file=f'candidate-{stamp}-background.wav' if package_wav else '',
            performance_file=performance, voice=voice_name, created=now(), synthesis=synthesis,
            config={key: row[key] for key in (*fields, 'spoken_text')})
        check_cancel(cancel)
        self.save_rows(identifier, {row['id']: dict(source_duration=round(original_duration, 3),
            candidates={**row['candidates'], stamp: candidate})})
        self.mark_done(identifier, [row['id']], phase='正在保存试听候选')

    def render_candidate(self, row, raw, settings, cache, log, cancel, source=None):
        if not reusable_voice_clip(raw):
            raise ValueError('配音服务未返回有效音频')
        preserve = bool(row.get('preserve_background') and source)
        channels = rate = 0
        backend = row.get('background_backend', 'demucs')
        if preserve:
            with wave.open(str(source), 'rb') as stream:
                channels, rate = stream.getnchannels(), stream.getframerate()
            if channels not in (1, 2, 6):
                raise ValueError('保留原声背景目前支持单声道、双声道或 5.1 语音')
        stamp = fingerprint([raw.name, {key: row[key] for key in TUNING}, preserve,
                             source.name if preserve else '', ['full-length-mix-v3', backend] if preserve else ''])
        output = cache / f'candidate-{stamp}.wav'
        full = cache / f'candidate-{stamp}-full.wav'
        voice = cache / f'candidate-{stamp}-voice.wav'
        bed = cache / f'candidate-{stamp}-background.wav'
        if output.is_file() and (not preserve or full.is_file() and voice.is_file() and bed.is_file()):
            return output, stamp, full if preserve else None, wav_duration(voice if voice.is_file() else output)
        end = wav_duration(raw) - row['trim_end']
        if end - row['trim_start'] < .08:
            raise ValueError('裁剪后音频少于 80 毫秒，请减小裁剪范围')
        filters = [f"atrim=start={row['trim_start']}:end={end}", 'asetpts=PTS-STARTPTS']
        if row['speed'] != 1:
            filters.append(f"atempo={row['speed']}")
        if row['gain']:
            filters.append(f"volume={row['gain']}dB")
        if row['lead']:
            filters.append(f"adelay={round(row['lead'] * 1000)}:all=1")
        if row['tail']:
            filters.append(f"apad=pad_dur={row['tail']}")
        pending = output.with_suffix('.pending.wav')
        if not preserve:
            try:
                ffmpeg(settings, ['-i', raw, '-af', ','.join(filters), '-ac', '1', '-ar', '24000',
                                  '-c:a', 'pcm_s16le', pending], log, cancel)
                check_cancel(cancel)
                os.replace(pending, output)
            finally:
                pending.unlink(missing_ok=True)
            return output, stamp, None, wav_duration(output)
        pending_voice = voice.with_suffix('.pending.wav')
        pending_full = full.with_suffix('.pending.wav')
        pending_bed = bed.with_suffix('.pending.wav')
        try:
            ffmpeg(settings, ['-i', raw, '-af', ','.join(filters), '-ac', '1', '-ar', str(rate),
                              '-c:a', 'pcm_s16le', pending_voice], log, cancel)
            check_cancel(cancel)
            os.replace(pending_voice, voice)
            if channels == 6:
                background = source
                background_filter = 'pan=5.1|FL=c0|FR=c1|FC=0*c0|LFE=c3|BL=c4|BR=c5'
                voice_route = 'pan=5.1|FL=0*c0|FR=0*c0|FC=c0|LFE=0*c0|BL=0*c0|BR=0*c0'
            else:
                background = self.background(cache.parent.name, source, settings, cancel, backend)
                background_filter = 'pan=mono|c0=0.5*c0+0.5*c1' if channels == 1 else 'anull'
                voice_route = 'anull' if channels == 1 else 'pan=stereo|FL=c0|FR=c0'
            ffmpeg(settings, ['-i', background, '-af', background_filter, '-ac', str(channels), '-ar', str(rate),
                              '-c:a', 'pcm_s16le', pending_bed], log, cancel)
            check_cancel(cancel)
            os.replace(pending_bed, bed)
            graph = f'[0:a]{voice_route}[voice];[1:a][voice]amix=inputs=2:duration=longest:normalize=0[a]'
            ffmpeg(settings, ['-i', voice, '-i', bed, '-filter_complex', graph, '-map', '[a]',
                              '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', pending_full], log, cancel)
            check_cancel(cancel)
            os.replace(pending_full, full)
            ffmpeg(settings, ['-i', full, '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', pending], log, cancel)
            check_cancel(cancel)
            os.replace(pending, output)
        finally:
            pending.unlink(missing_ok=True)
            pending_voice.unlink(missing_ok=True)
            pending_full.unlink(missing_ok=True)
            pending_bed.unlink(missing_ok=True)
        return output, stamp, full, wav_duration(voice)

    def package(self, identifier, rows, release, settings, cancel):
        data = self.state(identifier)
        folder = self.folder(identifier)
        sound = Path(data['sound'])
        _, encoder, template = tools()
        label = '正式' if release else '测试'
        package_id = uuid.uuid4().hex
        name = f"{data['title']} · {label} MOD"
        with TemporaryDirectory(prefix='mod-', dir=folder) as temporary:
            staging = Path(temporary)
            replacements = {}
            for index, row in enumerate(rows):
                check_cancel(cancel)
                self.update_job(identifier, phase='正在编码 Wwise 语音', done=index)
                candidate = row['candidates'].get(row['selected']) or {}
                wav = safe_file(folder / 'cache', candidate.get('package_file') or candidate['file'])
                asset = row['assets'][data['language']]
                codec, channels, rate = wem_format(extract_wem(sound, asset))
                if codec != 0xffff:
                    raise ValueError(f"资源 {row['id']} 的原声并非 Wwise Vorbis，当前打包器需要同编码语音")
                wav_input, wem_output = staging / 'input.wav', staging / f"{row['id']}.wem"
                log = folder / 'process.log'
                if candidate.get('package_file'):
                    ffmpeg(settings, ['-i', wav, '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', wav_input], log, cancel)
                else:
                    filters = ['-af', 'pan=5.1|FC=c0'] if channels == 6 else []
                    ffmpeg(settings, ['-i', wav, *filters, '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', wav_input], log, cancel)
                encode_wem(encoder, template, wav_input, wem_output, log, cancel)
                replacements[int(row['id'])] = dict(wem=wem_output, label=' / '.join(filter(None, (row['scene'], row['role']))) or row['id'],
                                                   tested=row['tested'])
            self.update_job(identifier, phase='正在生成 MOD 包', done=len(rows))
            temporary_zip = staging / 'package.zip'
            make_mod(sound, data['index_hash'], data['language'], name, replacements, temporary_zip, cancel,
                     build=data['build'], release=release)
            check_cancel(cancel)
            packages = folder / 'packages'
            packages.mkdir(exist_ok=True)
            os.replace(temporary_zip, packages / f'{package_id}.zip')
        with self.lock:
            data = self.state(identifier)
            data['packages'].append(dict(id=package_id, release=release, created=now(), count=len(rows),
                                         clips={row['id']: row['selected'] for row in rows}))
            self.write(identifier, data)


def validated_update(row, patch):
    emotion_fields(patch)
    for language in ('ja', 'en'):
        if patch[language] != row[language]:
            patch[language + '_source'] = 'manual' if patch[language] else ''
    if patch['text'] != row['text']:
        patch['text_source'] = 'manual' if patch['text'] else ''
    return patch
