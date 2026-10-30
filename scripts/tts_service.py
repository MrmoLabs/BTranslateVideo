"""Persistent stdio worker. EOF on the parent-owned pipe ends its lifetime."""
import hashlib
import json
import traceback
from pathlib import Path


def refresh_reference_cache(model, clips, signatures):
    for clip in clips:
        reference = clip['reference']
        digest = hashlib.sha256(Path(reference).read_bytes()).hexdigest()
        if reference in signatures and signatures[reference] != digest:
            if getattr(model, 'cache_spk_audio_prompt', None) == reference:
                model.cache_spk_audio_prompt = None
                model.cache_spk_cond = None
            if getattr(model, 'cache_emo_audio_prompt', None) == reference:
                model.cache_emo_audio_prompt = None
                model.cache_emo_cond = None
        signatures[reference] = digest


def serve(folder):
    import sys
    import torch
    from filelock import FileLock, Timeout
    from model_worker import load_tts, tts, write
    folder = Path(folder)
    parent = Path(__file__).resolve().parents[1]
    # The model retains GPU ownership while warm, not just during generation.
    lock = FileLock(parent/'runtime/gpu-model.lock')
    model = None
    signatures = {}
    acquired = False
    try:
        for line in sys.stdin:
            message = json.loads(line)
            request = json.loads(Path(message['request']).read_text(encoding='utf-8'))
            try:
                if not acquired:
                    write(folder/'state.json', dict(phase='等待 GPU'))
                    while not acquired:
                        try:
                            lock.acquire(timeout=.2)
                            acquired = True
                        except Timeout:
                            pass
                reused = model is not None
                load_seconds = 0
                if model is None:
                    write(folder/'state.json', dict(phase='加载模型'))
                    model, load_seconds = load_tts(request)
                refresh_reference_cache(model, request['clips'], signatures)
                write(folder/'state.json', dict(phase='正在合成'))
                torch.cuda.reset_peak_memory_stats()
                tts(request, model=model, load_seconds=load_seconds, reused=reused)
                torch.cuda.empty_cache()
                write(folder/'state.json', dict(phase='保温中'))
                write(message['result'], dict(id=message['id'], ok=True))
            except Exception as exc:
                traceback.print_exc()
                write(message['result'], dict(id=message['id'], error=str(exc)[-4000:] or type(exc).__name__))
                return  # Do not retain a model with a failed CUDA context.
    finally:
        if acquired:
            lock.release()
