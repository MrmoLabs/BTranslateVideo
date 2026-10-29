"""Own one local TTS worker and its bounded idle lifetime; no network listener."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import ROOT, atomic_json
from .media import check_cancel, stop_process


def model_identity(request, python):
    paths = [Path(python), Path(request['index_root']) / 'indextts/infer_v2_5.py',
             Path(request['index_root']) / 'indextts/infer_v2.py',
             ROOT / 'scripts/model_worker.py', ROOT / 'scripts/tts_service.py', ROOT / 'scripts/tts_batch.py']
    model = Path(request['model'])
    paths += sorted(p for p in model.rglob('*') if p.is_file() and '.cache' not in p.parts)
    records = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.is_file()]
    return hashlib.sha256(json.dumps([request['index_root'], request['model'], request['version'], str(python), records]).encode()).hexdigest()


class TTSSession:
    def __init__(self, root=ROOT, popen=subprocess.Popen, clock=time.monotonic):
        self.root, self.popen, self.clock = Path(root), popen, clock
        self.guard = threading.RLock()
        self.process = None
        self.key = None
        self.folder = None
        self.log = None
        self.timer = None
        self.deadline = 0
        self.running = False
        self.closed = False
        self.reason = '尚未加载'

    def status(self):
        process = self.process
        alive = process is not None and process.poll() is None
        phase = '未加载'
        if alive:
            phase = '正在合成' if self.running else '保温中'
            if self.running and self.folder:
                try:
                    phase = json.loads((self.folder / 'state.json').read_text(encoding='utf-8')).get('phase', phase)
                except (OSError, ValueError):
                    phase = '启动中'
        return dict(alive=alive, busy=self.running, phase=phase,
                    pid=process.pid if alive else None,
                    idle_seconds=max(0, round(self.deadline-self.clock())) if alive and not self.running else None,
                    reason=self.reason)

    def _stop(self, reason):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.process:
            stop_process(self.process)
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass  # A crashed worker may already have closed its pipe.
        if self.log:
            self.log.close()
        self.process = self.log = self.key = None
        self.deadline = 0
        self.reason = reason

    def release(self, reason='手动释放显存', blocking=True):
        if not self.guard.acquire(blocking=blocking):
            raise RuntimeError('配音模型正在工作，请先完成或取消当前任务')
        try:
            self._stop(reason)
        finally:
            self.guard.release()

    def close(self):
        with self.guard:
            self.closed = True
            self._stop('应用已关闭')

    def _schedule_expiry(self):
        # Timer waits can finish before the monotonic deadline on Windows.
        self.timer = threading.Timer(max(.01, self.deadline-self.clock()),
                                     self._expire, args=(self.deadline,))
        self.timer.daemon = True
        self.timer.start()

    def _expire(self, deadline):
        with self.guard:
            # Ignore a cancelled callback that waited behind a newer task.
            if self.running or not self.process or deadline != self.deadline:
                return
            if self.clock() >= deadline:
                self._stop('空闲保温已到期，自动释放显存')
            else:
                self._schedule_expiry()

    def run(self, request_path, python, worker, log_path, cancel, env, idle_seconds, on_tick=None):
        with self.guard:
            if self.closed:
                raise RuntimeError('配音会话已关闭')
            check_cancel(cancel)
            request = json.loads(Path(request_path).read_text(encoding='utf-8'))
            key = model_identity(request, python)
            if self.timer:
                self.timer.cancel()
                self.timer = None
            reused = bool(self.process and self.process.poll() is None and key == self.key)
            if not reused:
                self._stop('模型或运行环境发生变化')
                self.folder = self.root / 'runtime/tts-sessions' / uuid.uuid4().hex
                self.folder.mkdir(parents=True)
                self.log = (self.folder / 'process.log').open('ab', buffering=0)
                try:
                    self.process = self.popen([str(python), str(worker), 'serve', str(self.folder)],
                        stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8', env={**env, 'PYTHONUNBUFFERED':'1'},
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                except BaseException:
                    self._stop('模型进程启动失败')
                    raise
                self.key = key
            self.running = True
            token = uuid.uuid4().hex
            result_path = self.folder / (token + '.json')
            session_log = self.folder / 'process.log'
            offset = session_log.stat().st_size
            started = self.clock()

            def collect_log():
                nonlocal offset
                with session_log.open('rb') as source:
                    source.seek(offset)
                    data = source.read()
                    offset = source.tell()
                if data:
                    with Path(log_path).open('ab') as output:
                        output.write(data)

            try:
                self.process.stdin.write(json.dumps(dict(id=token, request=str(Path(request_path).resolve()), result=str(result_path)))+'\n')
                self.process.stdin.flush()
                while True:
                    check_cancel(cancel)
                    collect_log()
                    if on_tick:
                        on_tick()
                    if result_path.exists():
                        result = json.loads(result_path.read_text(encoding='utf-8'))
                        if result.get('id') != token:
                            raise RuntimeError('配音会话响应不匹配')
                        if result.get('error'):
                            raise RuntimeError(result['error'])
                        break
                    if self.process.poll() is not None:
                        raise RuntimeError('配音模型进程已退出，请重试；已完成片段可复用')
                    if self.clock()-started > 86400:
                        raise RuntimeError('配音任务超时')
                    cancel.wait(.15)
                collect_log()
                self.reason = '复用已加载模型' if reused else '本次首次加载模型'
            except BaseException:
                self._stop('任务取消、失败或进程中断，已释放显存')
                raise
            finally:
                self.running = False
            self.deadline = self.clock()+idle_seconds
            if idle_seconds <= 0:
                self._stop('已按设置在任务后释放')
            else:
                self._schedule_expiry()
