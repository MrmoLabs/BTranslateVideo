"""Build the optional local NieR dialogue lookup for the game workbench.

Inputs are the public NierDocs dialogue export and F-SERVO's Wwise name map.
Both are downloaded separately; this script deliberately performs no network
access. The output maps Wwise media IDs to Japanese, English and (converted)
Simplified Chinese text plus a speaker name.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIALOGUE = ROOT / 'runtime/game-audio-tools/allDialogueLines.json'
DEFAULT_NAMES = ROOT / 'runtime/game-audio-tools/wemIdsToNames.dart'
DEFAULT_OPENCC = ROOT / 'runtime/game-audio-tools/TSCharacters.txt'
DEFAULT_OUTPUT = ROOT / 'app/game-data/nier-dialogue.json'


def name_map(path: Path) -> dict[int, str]:
    text = path.read_text(encoding='utf-8')
    return {int(key): value for key, value in re.findall(r'(\d+):\s*"((?:[^"\\]|\\.)*)"', text)}


def simplified_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    table = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        key, _, values = line.partition('\t')
        first = values.split()
        if key and first:
            table[key] = first[0]
    return table


def simplify(text: str, table: dict[str, str]) -> str:
    return ''.join(table.get(char, char) for char in text) if table else text


def normalise(value: str) -> str:
    return re.sub(r'\s*\{[^}]*\}\s*$', '', value).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dialogue', type=Path, default=DEFAULT_DIALOGUE)
    parser.add_argument('--names', type=Path, default=DEFAULT_NAMES)
    parser.add_argument('--opencc', type=Path, default=DEFAULT_OPENCC)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    dialogue = json.loads(args.dialogue.read_text(encoding='utf-8'))
    names = name_map(args.names)
    conversions = simplified_map(args.opencc)
    by_name: dict[str, list[int]] = {}
    for media_id, name in names.items():
        by_name.setdefault(normalise(name), []).append(media_id)

    rows = {}
    for item in dialogue.values():
        event = item.get('audioEvent')
        if not event:
            continue
        media_ids = by_name.get(event) or by_name.get(normalise(event)) or []
        translations = item.get('translations') or {}
        record = {}
        for key, language in (('ja', 'Language.jpn'), ('en', 'Language.eng'), ('zh', 'Language.cht')):
            value = (translations.get(language) or '').strip()
            if value:
                record[key] = simplify(value, conversions) if key == 'zh' else value
        role = (item.get('name') or '').strip()
        if role:
            record['role'] = role
        if not record:
            continue
        record['source'] = item.get('source') or ''
        for media_id in media_ids:
            rows[str(media_id)] = record

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if args.output.suffix == '.gz':
        with args.output.open('wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=9, mtime=0) as stream:
                stream.write(payload)
    else:
        args.output.write_bytes(payload)
    print(f'{len(rows)} media IDs -> {args.output} ({args.output.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
