"""Offline, bounded-memory original-dialogue separation with overlap crossfades."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from model_worker import block_network


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def separate(request):
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.htdemucs import HTDemucs
    from safetensors.torch import load_file

    root = Path(request["model"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    weights = root / "htdemucs.safetensors"
    if hashlib.sha256(weights.read_bytes()).hexdigest() != manifest["weights_sha256"]:
        raise ValueError("背景分离模型校验失败，请重新准备模型")
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    model = HTDemucs(*config["args"], **config["kwargs"])
    model.load_state_dict(load_file(str(weights)))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    rate, channels = model.samplerate, model.audio_channels
    block_frames, overlap_frames = rate * 30, rate
    target_frames = round(request["duration"] * rate)
    output = Path(request["output"])
    pending = output.with_suffix(".pending.flac")
    decoder_log = output.with_suffix(".decode.log")
    print(f"BACKGROUND device={device}, streaming=30s, stereo={channels}", flush=True)
    with decoder_log.open("wb") as log:
        decoder = subprocess.Popen([request["ffmpeg"], "-hide_banner", "-nostdin", "-loglevel", "error", "-i", request["source"], "-map", "0:a:0", "-vn", "-ac", str(channels), "-ar", str(rate), "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            def read_frames(count):
                needed, data = count * channels * 4, bytearray()
                while len(data) < needed:
                    part = decoder.stdout.read(needed - len(data))
                    if not part: break
                    data.extend(part)
                if len(data) % (channels * 4): raise ValueError("原音频解码帧不完整")
                return np.frombuffer(data, dtype="<f4").reshape(-1, channels).copy()

            written, tail = 0, None
            with sf.SoundFile(str(pending), "w", samplerate=rate, channels=channels, subtype="PCM_24", format="FLAC") as writer:
                def write(data):
                    nonlocal written
                    data = data[:max(0, target_frames - written)]
                    if len(data): writer.write(data); written += len(data)
                chunk = read_frames(block_frames)
                if not len(chunk): raise ValueError("原音轨为空")
                while len(chunk):
                    tensor = torch.from_numpy(chunk.T.copy())
                    mean, std = tensor.mean(), tensor.mean(0).std()
                    if float(std) < 1e-7:
                        bed = chunk
                    else:
                        with torch.inference_mode():
                            estimates = apply_model(model, ((tensor - mean) / std)[None], device=device, shifts=0, split=True, overlap=.25, segment=6, progress=False, num_workers=0)[0]
                            vocal = estimates[model.sources.index("vocals")].cpu() * std + mean
                            bed = (tensor - vocal).T.numpy()
                    if tail is not None:
                        n = min(len(tail), len(bed), overlap_frames)
                        ramp = np.linspace(0, 1, n, dtype=np.float32)[:, None]
                        write(tail[-n:] * (1 - ramp) + bed[:n] * ramp)
                        bed = bed[n:]
                    keep = min(overlap_frames, len(bed))
                    if keep:
                        write(bed[:-keep]); tail = bed[-keep:].copy()
                    else: tail = None
                    atomic_json(request["progress"], dict(frames=written, total=target_frames, device=device))
                    print(f"BACKGROUND {written}/{target_frames}", flush=True)
                    extra = read_frames(block_frames - overlap_frames)
                    if not len(extra):
                        if tail is not None: write(tail)
                        break
                    chunk = np.concatenate((chunk[-overlap_frames:], extra), axis=0)
                while written < target_frames:
                    write(np.zeros((min(rate, target_frames - written), channels), dtype=np.float32))
            if decoder.wait(timeout=20): raise RuntimeError("原音轨解码失败，请查看背景分离日志")
            os.replace(pending, output)
            atomic_json(request["progress"], dict(frames=target_frames, total=target_frames, device=device))
            print("BACKGROUND complete", flush=True)
        finally:
            if decoder.poll() is None:
                decoder.terminate()
                try: decoder.wait(timeout=5)
                except subprocess.TimeoutExpired: decoder.kill(); decoder.wait()
            if decoder.stdout: decoder.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    block_network()
    from filelock import FileLock
    with FileLock(Path(__file__).resolve().parents[1] / "runtime" / "gpu-model.lock", timeout=86400):
        separate(request)
