"""Heavy models live in short-lived subprocesses, releasing GPU memory per stage."""
from __future__ import annotations

import argparse
import json
import os
import sys
import site
import wave
import socket
import time
from pathlib import Path

DLL_HANDLES = []
if os.name == "nt":
    for package_dir in site.getsitepackages():
        for directory in Path(package_dir).glob("nvidia/*/bin"):
            DLL_HANDLES.append(os.add_dll_directory(str(directory)))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


def block_network():
    original_connect = socket.socket.connect
    def offline_connect(connection, address):
        if connection.family in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("模型推理进程禁止联网。请先通过 setup.ps1 准备全部本地文件。")
        return original_connect(connection, address)
    socket.socket.connect = offline_connect


def write(path, value):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def asr(request):
    from faster_whisper import WhisperModel
    import numpy as np

    model = WhisperModel(request["model"], device=request["device"], compute_type=request["compute_type"], local_files_only=True)
    with wave.open(request["audio"], "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError("ASR 需要预处理为 16kHz 单声道 PCM16")
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    segments, info = model.transcribe(audio, language=request["language"] or None, task="transcribe", vad_filter=True, word_timestamps=True, beam_size=5, condition_on_previous_text=False, hallucination_silence_threshold=1.0)
    output = []
    for segment in segments:
        text = segment.text.strip()
        if text and segment.end > segment.start:
            words = [dict(word=w.word, start=round(max(0, w.start), 3), end=round(min(info.duration, w.end), 3)) for w in (segment.words or []) if w.word.strip()]
            output.append(dict(id=len(output) + 1, start=round(max(0, segment.start), 3), end=round(min(info.duration, segment.end), 3), text=text, zh="", speaker="speaker_1", words=words))
            print(f"ASR {segment.end:.1f}s: {text}", flush=True)
    write(request["output"], output)


def tts(request):
    started=time.perf_counter()
    from tts_batch import report
    report(request.get('progress'),dict(phase='加载模型',instances=1,completed=0,total=len(request['clips']),requested_batch=request.get('batch_size',1),actual_batch=0))
    root = Path(request["index_root"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    auxiliary = Path(request["model"]) / "hf_cache"
    required = [auxiliary / "w2v-bert-2.0" / "config.json", auxiliary / "w2v-bert-2.0" / "model.safetensors", auxiliary / "campplus_cn_common.bin", auxiliary / "bigvgan" / "config.json", auxiliary / "bigvgan" / "bigvgan_generator.pt"]
    if any(not path.is_file() for path in required):
        raise RuntimeError("IndexTTS 辅助模型尚未准备，请重新运行 setup.ps1；离线处理不会自动联网下载")
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("配音环境中 CUDA 不可用，请安装 CUDA 版 PyTorch 并检查显卡；当前配置要求 GPU 配音")
    if request["version"] == "2.5":
        from indextts.infer_v2_5 import IndexTTS2
        model = IndexTTS2(cfg_path=str(Path(request["model"]) / "config.yaml"), model_dir=request["model"], use_bf16=True, use_cuda_kernel=False, use_deepspeed=False)
    else:
        from indextts.infer_v2 import IndexTTS2
        model = IndexTTS2(cfg_path=str(Path(request["model"]) / "config.yaml"), model_dir=request["model"], use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
    if request["version"] == "2.5":
        from tts_batch import run_clips
        run_clips(model, request["clips"], request.get("batch_size", 1), request.get("stats"), request.get('progress'), time.perf_counter()-started)
        return
    for i, clip in enumerate(request["clips"], 1):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from app.emotions import emotion_kwargs
        kwargs = dict(spk_audio_prompt=clip["reference"], text=clip["text"], output_path=clip["output"] + ".pending.wav", verbose=False)
        if request["version"] == "2.5":
            kwargs["lang"] = "ZH"
        model.infer(**kwargs,**emotion_kwargs(clip))
        os.replace(kwargs["output_path"], clip["output"])
        print(f"TTS {i}/{len(request['clips'])} id={clip['id']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["asr", "tts"])
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    block_network()
    from filelock import FileLock
    with FileLock(Path(__file__).resolve().parents[1] / "runtime" / "gpu-model.lock", timeout=86400):
        (asr if args.kind == "asr" else tts)(request)
