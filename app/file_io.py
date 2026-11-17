"""Atomic UTF-8 JSON with bounded Windows sharing retries.

Project state/results are durable and propagate write errors. Progress snapshots
are replaceable telemetry: retain the last snapshot if publishing is blocked.
This module deliberately uses only the standard library for model environments.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import time


RETRY_DELAYS = (.01, .02, .04, .08, .12, .16)
PROGRESS_RETRY_DELAYS = (.005, .01, .02)
_warning_times: dict[str, float] = {}
_logger = logging.getLogger(__name__)


def atomic_json(path: Path, value, *, retry_delays=RETRY_DELAYS, sync=True, indent=2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=indent, allow_nan=False)
            stream.flush()
            if sync:os.fsync(stream.fileno())
        for attempt in range(len(retry_delays) + 1):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                # A reader/antivirus handle without FILE_SHARE_DELETE can cause
                # access denied, sharing violation or lock violation on Windows.
                # Do not retry unrelated failures or hide persistent ACL errors.
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == len(retry_delays):raise
                time.sleep(retry_delays[attempt])
    finally:
        try:Path(temporary).unlink(missing_ok=True)
        except OSError:
            # Cleanup must not turn a successful replace into a job failure or
            # mask the original exception when a scanner also holds the temp.
            _logger.debug('Could not remove temporary JSON %s', temporary, exc_info=True)


def write_progress(path, value) -> bool:
    if not path:return True
    try:
        atomic_json(Path(path), value, retry_delays=PROGRESS_RETRY_DELAYS, sync=False, indent=None)
        return True
    except OSError as exc:
        key=str(path);now=time.monotonic()
        if key not in _warning_times or now-_warning_times[key]>=30:
            if len(_warning_times)>=128:_warning_times.pop(next(iter(_warning_times)))
            _warning_times[key]=now
            _logger.warning('Progress snapshot unavailable; retaining the previous snapshot and continuing processing (%s): %s', path, exc)
        return False
