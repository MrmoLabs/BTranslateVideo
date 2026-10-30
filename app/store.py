from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA, atomic_json


def now():
    return datetime.now(timezone.utc).isoformat()


class ProjectUnavailable(FileNotFoundError):
    def __init__(self, identifier, *, deleted=False, revision=None):
        super().__init__(identifier)
        self.identifier, self.deleted, self.revision = identifier, deleted, revision


class Store:
    def __init__(self, root: Path = DATA):
        self.root = root
        self.lock = threading.RLock()
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
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise ProjectUnavailable(identifier) from exc
            if data.get("deleted_at") and not include_deleted:
                raise ProjectUnavailable(identifier, deleted=True, revision=data.get("metadata_revision", 0))
            return data

    def update(self, identifier: str, **values):
        with self.lock:
            data = self.read(identifier, include_deleted=True)
            if "segments" in values and values["segments"] != data.get("segments"):
                values["edit_revision"] = data.get("edit_revision", 0) + 1
            data.update(values, updated=now())
            atomic_json(self.folder(identifier) / "project.json", data)
            return copy.deepcopy(data)

    def create(self, name: str, language: str, mode: str, target_language: str = "zh") -> dict:
        identifier = uuid.uuid4().hex
        data = dict(id=identifier, name=name[:160], language=language, target_language=target_language, mode=mode, created=now(), updated=now(), status="idle", stage="", progress=0, error="", segments=[], edit_revision=0, references={}, background="", background_mode="original", background_gain=1.0, outputs={}, logs=[], media={})
        atomic_json(self.folder(identifier) / "project.json", data)
        return data

    def log(self, identifier: str, message: str):
        with self.lock:
            data = self.read(identifier)
            logs = data.get("logs", []) + [dict(time=now(), message=message)]
            self.update(identifier, logs=logs[-200:])

    def list(self, *, deleted: bool = False) -> list[dict]:
        result = []
        for folder in self.root.iterdir():
            if folder.is_dir() and (folder / "project.json").exists():
                try:
                    data = self.read(folder.name, include_deleted=True)
                    if bool(data.get("deleted_at")) != deleted:
                        continue
                    result.append({**{key: data[key] for key in ("id", "name", "language", "mode", "status", "stage", "progress", "updated", "error")}, "target_language": data.get("target_language", "zh"), "metadata_revision": data.get("metadata_revision", 0), "deleted_at": data.get("deleted_at")})
                except (ValueError, OSError, KeyError):
                    continue
        return sorted(result, key=lambda data: data["updated"], reverse=True)

    def recover(self):
        for item in self.list():
            if item["status"] in {"running", "queued", "cancelling"}:
                self.update(item["id"], status="interrupted", error="上次运行被中断，可重新执行；已完成片段会按内容校验后复用。")
