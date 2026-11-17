"""Conservative project-local cleanup with a short-lived, exact-file preview."""
from __future__ import annotations
import os
import time
import uuid
from pathlib import Path
from fastapi import HTTPException
from pydantic import BaseModel, Field

CATEGORIES = ('temporary', 'analysis', 'history')
ANALYSIS_PREFIXES = ('sentences-', 'translation-', 'asr-', 'diarization-', 'tts-progress')


def plain_path(path, root):
    """Reject links/junctions at every component, including the project root."""
    if not path.is_relative_to(root):
        raise ValueError('文件超出项目目录')
    for item in (root, *reversed(path.relative_to(root).parents), path.relative_to(root)):
        target = item if item.is_absolute() else root / item
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
            raise ValueError('清理不支持符号链接或目录联接')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('文件超出项目目录')


def fingerprint(path):
    value = path.stat()
    return [value.st_size, value.st_mtime_ns, value.st_ino]


class ProjectStorage:
    def __init__(self, store):
        self.store = store
        self.plans = {}

    def inventory(self, identifier):
        project = self.store.read(identifier)
        root = self.store.folder(identifier)
        plain_path(root, root)
        # All strings in current metadata are protected if they name a file.
        protected = set()
        def collect(value):
            if isinstance(value, dict):
                for item in value.values(): collect(item)
            elif isinstance(value, list):
                for item in value: collect(item)
            elif isinstance(value, str) and 0 < len(value) < 260:
                candidate = root / value
                try:
                    if candidate.is_relative_to(root) and candidate.is_file(): protected.add(candidate.relative_to(root).as_posix())
                except (OSError, ValueError): pass
        for key in ('references','outputs','audition','background','diarization_proposal','translation_proposal'):
            collect(project.get(key))
        current_review = str((project.get('subtitle_review') or {}).get('id', ''))
        history = root / 'history'
        histories=[]
        try:
            plain_path(history,root)
            if history.is_dir():
                for path in history.iterdir():
                    try:plain_path(path,root)
                    except ValueError:continue
                    if path.is_dir():histories.append(path)
            histories.sort()
        except ValueError:pass
        newest = histories[-1].name if histories else None
        totals = dict(source=0, references=0, results=0, cache=0, history=0, other=0)
        eligible = {name: [] for name in CATEGORIES}
        skipped = 0
        for folder, directories, names in os.walk(root, followlinks=False):
            safe = []
            for name in directories:
                try: plain_path(Path(folder) / name, root); safe.append(name)
                except ValueError: skipped += 1
            directories[:] = safe
            for name in names:
                path = Path(folder) / name
                try:
                    plain_path(path, root); info = fingerprint(path)
                except (ValueError, FileNotFoundError): skipped += 1; continue
                relative = path.relative_to(root).as_posix()
                parts = Path(relative).parts
                bucket = 'cache' if parts[0] == 'cache' else 'history' if parts[0] == 'history' else 'source' if name == 'source.media' else 'references' if name.startswith(('reference-', 'voice-', 'speaker-')) else 'results' if path.suffix in {'.wav', '.mp4', '.mkv', '.srt', '.vtt'} else 'other'
                totals[bucket] += info[0]
                category = None
                if relative in protected or parts[0] not in {'cache', 'history'}: continue
                # Never offer reusable sentence audio or live candidate ledgers.
                if name.startswith(('tts-', 'fit-')) and path.suffix == '.wav': continue
                if name.startswith('subtitle-review-') and (current_review in name if current_review else True): continue
                if parts[0] == 'cache':
                    if '.invalid-' in name or '.pending.' in name or path.suffix == '.tmp': category = 'temporary'
                    elif path.suffix == '.json' and (name in {'asr.json', 'asr-words-metadata.json'} or name.startswith(ANALYSIS_PREFIXES)): category = 'analysis'
                elif len(parts) > 2 and parts[1] != newest:
                    category = 'history'
                if category: eligible[category].append(dict(path=relative, bytes=info[0], fingerprint=info))
        return dict(project_id=identifier, revision=project.get('edit_revision', 0), totals=totals,
                    bytes=sum(totals.values()), skipped=skipped, newest_backup=newest,
                    categories={key: dict(count=len(items), bytes=sum(item['bytes'] for item in items)) for key, items in eligible.items()}), eligible

    def preview(self, identifier, categories, revision):
        if not categories or set(categories) - set(CATEGORIES): raise ValueError('请选择有效的清理范围')
        inventory, eligible = self.inventory(identifier)
        if revision != inventory['revision']: raise HTTPException(409, '项目已变化，请保存并重新预览')
        files = sorted((item for category in set(categories) for item in eligible[category]), key=lambda item: item['path'])
        self.plans = {key: value for key, value in self.plans.items() if value['expires'] > time.monotonic()}
        if len(self.plans) >= 32: self.plans.pop(next(iter(self.plans)))
        token = uuid.uuid4().hex
        plan = dict(project=identifier, revision=revision, files=files, categories=sorted(set(categories)), expires=time.monotonic() + 600)
        self.plans[token] = plan
        return dict(token=token, count=len(files), bytes=sum(item['bytes'] for item in files),
                    files=[{key: item[key] for key in ('path', 'bytes')} for item in files], expires_seconds=600)

    def clean(self, identifier, token, revision):
        plan = self.plans.pop(token, None)
        if not plan or plan['expires'] < time.monotonic() or plan['project'] != identifier or plan['revision'] != revision:
            raise HTTPException(409, '清理预览已失效，请重新预览')
        current, eligible = self.inventory(identifier)
        allowed = {item['path']: item for category in plan['categories'] for item in eligible[category]}
        if current['revision'] != revision or any(allowed.get(item['path']) != item for item in plan['files']):
            raise HTTPException(409, '文件或项目已变化，未清理任何文件，请重新预览')
        removed = []; error = ''
        for item in plan['files']:
            path = self.store.folder(identifier) / item['path']
            try:
                plain_path(path, self.store.folder(identifier))
                if fingerprint(path) != item['fingerprint']: raise OSError('文件已变化')
                path.unlink(); removed.append(item)
            except (OSError,ValueError) as exc:
                error = f"{item['path']}: {exc}"; break
        return dict(removed=len(removed), bytes=sum(item['bytes'] for item in removed), error=error,
                    inventory=self.inventory(identifier)[0])


class StoragePreview(BaseModel):
    categories: list[str] = Field(min_length=1, max_length=3)
    revision: int = Field(ge=0)

class StorageClean(BaseModel):
    token: str = Field(pattern=r'^[0-9a-f]{32}$')
    revision: int = Field(ge=0)


def attach(app, store, pipeline):
    service = ProjectStorage(store)
    def editable(identifier):
        store.read(identifier)
        if pipeline.busy(identifier): raise HTTPException(409, '项目处理中，请结束任务后再管理存储')

    @app.get('/api/projects/{identifier}/storage')
    def inventory(identifier: str):
        with pipeline.lock, store.lock:
            return service.inventory(identifier)[0]

    @app.post('/api/projects/{identifier}/storage/preview')
    def preview(identifier: str, payload: StoragePreview):
        with pipeline.lock, store.lock:
            editable(identifier)
            return service.preview(identifier, payload.categories, payload.revision)

    @app.post('/api/projects/{identifier}/storage/clean')
    def clean(identifier: str, payload: StorageClean):
        with pipeline.lock, store.lock:
            editable(identifier)
            return service.clean(identifier, payload.token, payload.revision)
