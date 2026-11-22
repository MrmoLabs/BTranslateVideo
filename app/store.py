from __future__ import annotations

import copy
import shutil
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA, atomic_json
from .progress import ACTIVE, TERMINAL, finish
from .project_state import ProjectStateFiles,RUNTIME_FIELDS


def now():
    return datetime.now(timezone.utc).isoformat()


def is_junction(path: Path):
    return getattr(path.lstat(), 'st_reparse_tag', None) == stat.IO_REPARSE_TAG_MOUNT_POINT


class ProjectUnavailable(FileNotFoundError):
    def __init__(self, identifier, *, deleted=False, revision=None):
        super().__init__(identifier)
        self.identifier, self.deleted, self.revision = identifier, deleted, revision


class Store:
    def __init__(self, root: Path = DATA):
        self.root = root
        self.lock = threading.RLock()
        self.state_files=ProjectStateFiles()
        root.mkdir(parents=True, exist_ok=True)

    def folder(self, identifier: str) -> Path:
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("无效项目编号")
        return self.root / identifier

    def read(self, identifier: str, *, include_deleted: bool = False) -> dict:
        import json
        with self.lock:
            path = self.folder(identifier) / "project.json"
            try:
                data,_ = self.state_files.read(path)
            except FileNotFoundError as exc:
                raise ProjectUnavailable(identifier) from exc
            if data.get("deleted_at") and not include_deleted:
                raise ProjectUnavailable(identifier, deleted=True, revision=data.get("metadata_revision", 0))
            return copy.deepcopy(data)

    def read_runtime(self,identifier):
        with self.lock:
            path=self.folder(identifier)/"project.json"
            try:data,_=self.state_files.read(path)
            except FileNotFoundError as exc:raise ProjectUnavailable(identifier) from exc
            if data.get('deleted_at'):raise ProjectUnavailable(identifier,deleted=True,revision=data.get('metadata_revision',0))
            return {key:copy.deepcopy(data[key]) for key in RUNTIME_FIELDS if key in data}

    def update_runtime(self,identifier,**values):
        if set(values)-RUNTIME_FIELDS:raise ValueError("运行快照不能修改项目内容")
        with self.lock:
            path=self.folder(identifier)/"project.json"
            data,digest=self.state_files.read(path)
            if data.get("deleted_at"):raise ProjectUnavailable(identifier,deleted=True)
            data.update(values,updated=now())
            self.state_files.write(path,data,values,digest)
            return {key:copy.deepcopy(data[key]) for key in RUNTIME_FIELDS if key in data}

    def update(self, identifier: str, **values):
        with self.lock:
            path=self.folder(identifier)/"project.json"
            data,digest=self.state_files.read(path)
            if "segments" in values and values["segments"] != data.get("segments"):
                values["edit_revision"] = data.get("edit_revision", 0) + 1
            data.update(values, updated=now())
            self.state_files.write(path,data,values,digest)
            return copy.deepcopy(data)

    def create(self, name: str, language: str, mode: str, target_language: str = "zh", goal: str = 'dub') -> dict:
        identifier = uuid.uuid4().hex
        data = dict(id=identifier, name=name[:160], language=language, target_language=target_language, mode=mode, created=now(), updated=now(), status="idle", stage="", progress=0, error="", segments=[], edit_revision=0, references={}, background="", background_mode="original", background_gain=1.0, outputs={}, logs=[], media={})
        data['goal']=goal
        atomic_json(self.folder(identifier) / "project.json", data)
        return data

    def log(self, identifier: str, message: str):
        with self.lock:
            data = self.read_runtime(identifier)
            logs = data.get("logs", []) + [dict(time=now(), message=message)]
            self.update_runtime(identifier, logs=logs[-200:])

    def purge(self, identifier: str):
        """Remove only a trashed project's own files, retaining its entry for retries."""
        with self.lock:
            folder = self.folder(identifier)
            expected = self.root.resolve() / identifier
            if folder.is_symlink() or is_junction(folder) or folder.resolve() != expected:
                raise ValueError('彻底删除仅允许项目目录中的普通文件夹')
            project = self.read(identifier, include_deleted=True)
            if not project.get('deleted_at'):
                raise ValueError('请先将项目移入回收站')
            manifest = folder / 'project.json'
            if manifest.is_symlink():
                raise ValueError('项目清单不能是符号链接')
            if not project.get('purge_started'):
                project = self.update(identifier, purge_started=True)
            for entry in folder.iterdir():
                if entry == manifest:
                    continue
                if entry.is_symlink():
                    entry.unlink(missing_ok=True)
                elif is_junction(entry):
                    entry.rmdir()
                elif entry.is_dir():
                    if entry.resolve() != expected / entry.name:
                        raise ValueError('目录指向项目范围之外，彻底删除已暂停')
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
            manifest.unlink()
            self.state_files.forget(manifest)
            try:
                folder.rmdir()
            except OSError:
                if folder.is_dir() and not folder.is_symlink() and not is_junction(folder) and folder.resolve() == expected:
                    atomic_json(manifest, project)
                raise

    def list(self, *, deleted: bool = False) -> list[dict]:
        result = []
        for folder in self.root.iterdir():
            if folder.is_dir() and (folder / "project.json").exists():
                try:
                    with self.lock:data,_=self.state_files.read(folder/"project.json")
                    if bool(data.get("deleted_at")) != deleted:
                        continue
                    from .project_experience import summary
                    result.append({**{key: data[key] for key in ("id", "name", "language", "mode", "status", "stage", "progress", "updated", "error")}, "target_language": data.get("target_language", "zh"), "metadata_revision": data.get("metadata_revision", 0), "deleted_at": data.get("deleted_at"), "purge_started": bool(data.get("purge_started")), "processing":data.get("processing"), 'goal':data.get('goal','dub'), 'workflow':summary(data)})
                except (ValueError, OSError, KeyError):
                    continue
        return sorted(result, key=lambda data: data["updated"], reverse=True)

    def recover(self):
        for item in self.list():
            progress=item.get('processing')
            if item['status'] in ACTIVE or progress and progress['status'] in ACTIVE:
                status=item['status'] if item['status'] in TERMINAL else 'interrupted'
                values=dict(status=status)
                if progress:values['processing']=finish(progress,status,at=progress['updated_at'])
                if status=='interrupted':values['error']='上次运行被中断，可重新执行；已完成片段会按内容校验后复用。'
                self.update_runtime(item['id'],**values)
