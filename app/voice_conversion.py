"""Seed-VC offline voice-conversion adapter.

The game workflow uses a native Chinese TTS voice so the pronunciation is
accent-free, then Seed-VC transfers the original character's timbre onto that
Chinese performance. Seed-VC lives in its own environment because its pinned
PyTorch stack differs from the application's.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .audio_cache import reusable_voice_clip
from .config import ROOT, resolve
from .file_io import atomic_json
from .media import Cancelled, check_cancel, run


def interpreter(settings):
    if settings.vc_python.strip():
        return resolve(settings.vc_python)
    return resolve(settings.vc_root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def worker_env(settings):
    """Seed-VC keeps its own Hugging Face cache; never reuse the app's offline env."""
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_TELEMETRY="1",
               TOKENIZERS_PARALLELISM="false", TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD="1")
    env["HF_HOME"] = str(resolve(settings.vc_root) / "hf-cache")
    env.pop("HF_HUB_OFFLINE", None)
    env.pop("TRANSFORMERS_OFFLINE", None)
    if settings.vc_hf_endpoint.strip():
        env["HF_ENDPOINT"] = settings.vc_hf_endpoint.strip()
    return env


def available(settings):
    if settings.vc_provider != "seed_vc":
        return False
    root = resolve(settings.vc_root)
    return interpreter(settings).is_file() and (root / "seed_vc_wrapper.py").is_file()


def convert(settings, clips, work, log, cancel, on_progress=None, on_fallback=None):
    """Convert every clip's timbre to its target reference, in one worker run."""
    if not clips:
        return
    if not available(settings):
        raise ValueError("变声需要先安装 Seed-VC 环境（vendor/seed-vc/.venv），或把变声接口设为 Seed-VC")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex
    request_path = work / f"request-{stamp}.json"
    progress_path = work / f"progress-{stamp}.json"
    payload = dict(
        root=str(resolve(settings.vc_root)),
        steps=settings.vc_steps,
        cfg_rate=settings.vc_cfg_rate,
        progress=str(progress_path),
        clips=[dict(id=clip["id"], source=str(clip["source"]), target=str(clip["target"]),
                    output=str(clip["output"])) for clip in clips],
    )
    atomic_json(request_path, payload)
    completed = 0

    def tick():
        if not on_progress:
            return
        try:
            value = json.loads(progress_path.read_text(encoding="utf-8"))
            on_progress(completed + int(value.get("done", 0)), len(clips))
        except (OSError, ValueError):
            pass

    command = [str(interpreter(settings)), str(ROOT / "scripts" / "seed_vc_worker.py"), str(request_path)]
    try:
        run(command, log, cancel, env=worker_env(settings), timeout=7200, on_tick=tick)
    except Cancelled:
        raise
    except RuntimeError as exc:
        error = str(exc).lower()
        if not any(marker in error for marker in ('cuda error', 'cuda out of memory', 'cublas_status_alloc_failed')):
            raise
        check_cancel(cancel)
        remaining = [clip for clip in payload['clips'] if not reusable_voice_clip(clip['output'])]
        completed = len(clips) - len(remaining)
        if remaining:
            if on_fallback:
                on_fallback()
            progress_path.unlink(missing_ok=True)
            atomic_json(request_path, {**payload, 'clips': remaining, 'device': 'cpu'})
            run(command, log, cancel, env=worker_env(settings), timeout=7200, on_tick=tick)
    check_cancel(cancel)
    for clip in clips:
        path = Path(clip["output"])
        if not reusable_voice_clip(path):
            raise ValueError(f"变声没有生成有效音频：{clip['id']}")
