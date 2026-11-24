"""Bounded, in-memory request history for the local task viewer."""
from collections import OrderedDict
from contextvars import ContextVar
from threading import Lock
from time import time
from uuid import uuid4


request_scope = ContextVar("translation_request_scope", default=None)
TEXT_LIMIT = 96_000
HISTORY_LIMIT = 24


class TranslationRequests:
    def __init__(self):
        self._lock = Lock()
        self._records = OrderedDict()

    def start(self, project, run, phase, ids, url, body):
        request_id = uuid4().hex[:16]
        with self._lock:
            self._records[request_id] = dict(id=request_id, project=project, run=run, phase=phase, ids=ids,
                url=url, request=body[:TEXT_LIMIT], response="", content="", error="", http_status=None,
                started=time(), first_response=None, finished=None, truncated=len(body)>TEXT_LIMIT)
            while len(self._records) > HISTORY_LIMIT:
                removable = next((key for key, row in self._records.items() if row["finished"] is not None), None)
                if removable is None:
                    break
                self._records.pop(removable)
        return request_id

    def update(self, request_id, **values):
        with self._lock:
            if request_id in self._records:
                self._records[request_id].update(values)

    def append(self, request_id, raw, content=""):
        with self._lock:
            row = self._records.get(request_id)
            if row is None:
                return
            if row["first_response"] is None:
                row["first_response"] = time()
            for field, value in (("response", raw), ("content", content)):
                remaining = TEXT_LIMIT - len(row[field])
                row[field] += value[:remaining]
                if len(value) > remaining:
                    row["truncated"] = True

    def snapshot(self, project, run, request_id=None):
        with self._lock:
            entries = [row for row in reversed(self._records.values()) if row["project"] == project and row["run"] == run]
            selected = next((row for row in entries if row["id"] == request_id), None)
            if selected is None and entries:
                selected = entries[0]
            hidden = {"project", "run", "request", "response", "content"}
            return dict(requests=[{key: value for key, value in row.items() if key not in hidden} for row in entries],
                        selected={key: value for key, value in selected.items() if key not in {"project", "run"}} if selected else None)
