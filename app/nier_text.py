"""Optional local NieR:Automata dialogue text keyed by Wwise media ID.

The app only indexes Wwise audio at scan time. Dialogue text lives in separate
CPK/SMD/script resources, so this optional lookup fills blank rows when the
user explicitly asks to import game dialogue. It never overwrites existing
edits.
"""
from __future__ import annotations

from functools import lru_cache
import gzip
import json
from pathlib import Path


FOLDER = Path(__file__).resolve().parent / 'game-data'
DATA = FOLDER / 'nier-dialogue.json'
LEGACY = FOLDER / 'nier-dialogue.json.gz'


def _read(path: Path):
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def dialogue_lines() -> dict[str, dict]:
    """Return ``media_id -> {ja,en,zh,role,source}``, or an empty mapping."""
    for path in (DATA, LEGACY):
        if not path.is_file():
            continue
        try:
            data = _read(path)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def dialogue_source() -> str:
    for path in (DATA, LEGACY):
        if path.is_file():
            return str(path)
    return ''
