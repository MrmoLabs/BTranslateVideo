"""Offline Fun-ASR-Nano worker. A batch shares one model and the GPU lock."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.file_io import atomic_json, write_progress
from app.fun_asr import language_name, timed_segment
from model_worker import block_network


def load_models(request):
    import torch
    from funasr import AutoModel
    # Register the implementation shipped in the pinned toolkit. Do not execute
    # repository Python or let FunASR pip-install model requirements at runtime.
    from funasr.models.fun_asr_nano.model import FunASRNano  # noqa: F401
    if request['device'] == 'cuda' and not torch.cuda.is_available():
        raise ValueError('Fun-ASR 环境的 CUDA 不可用，请重新准备 CUDA 版环境或在设置中选择 CPU')
    common = dict(device='cuda:0' if request['device'] == 'cuda' else 'cpu',
                  disable_update=True, check_latest=False, trust_remote_code=False, disable_pbar=True)
    model = AutoModel(model=request['model'], **common)
    if getattr(model.model, 'ctc_decoder', None) is None:
        raise ValueError('Fun-ASR 权重缺少已训练的 CTC 时间对齐层，请重新运行 prepare_fun_asr.py')
    vad = AutoModel(model=request['vad_model'], **{**common, 'device': 'cpu'}, max_single_segment_time=30000)
    return model, vad


def speech_windows(ranges, frames, rate=16000):
    """Bound each decode to 30 s, retaining the VAD's order and silence gaps."""
    previous = 0
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError('Fun-ASR VAD 返回了无效语音区间')
        start, end = max(0, round(float(pair[0]) * rate / 1000)), min(frames, round(float(pair[1]) * rate / 1000))
        if start < previous or end <= start:
            raise ValueError('Fun-ASR VAD 语音区间越界或顺序错误')
        while start < end:
            stop = min(end, start + 30 * rate)
            yield start, stop
            start = stop
        previous = end


def transcribe(request, models):
    import numpy as np
    import torch
    model, vad = models
    language = language_name(request.get('language'))
    progress = request.get('progress')
    with wave.open(str(request['audio']), 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Fun-ASR 需要 16kHz 单声道 PCM16 音频')
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
    duration = len(audio) / 16000
    write_progress(progress, dict(phase='asr_prepare', message='Fun-ASR 检测语音区间', done=0, total=duration, unit='seconds'))
    detected = vad.generate(input=audio, cache={}, batch_size=1)
    if not isinstance(detected, list) or len(detected) != 1 or not isinstance(detected[0].get('value'), list):
        raise ValueError('Fun-ASR VAD 未返回有效语音区间')
    output = []
    for start, end in speech_windows(detected[0]['value'], len(audio)):
        options = dict(cache={}, batch_size=1, itn=True)
        if language:
            options['language'] = language
        if request.get('hotwords'):
            options['hotwords'] = request['hotwords']
        # Nano's chat template accepts tensors or paths; its generic AutoModel
        # wrapper also advertises NumPy, which this checkpoint does not handle.
        result = model.generate(input=torch.from_numpy(audio[start:end]), **options)
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError('Fun-ASR 未返回本段识别结果')
        segment = timed_segment(result[0], start / 16000, (end - start) / 16000, len(output) + 1)
        if segment:
            output.append(segment)
            print(f"Fun-ASR {end / 16000:.1f}s: {segment['text']}", flush=True)
        write_progress(progress, dict(phase='asr_decode', message='Fun-ASR 识别原文与字级时间戳', done=end / 16000, total=duration, unit='seconds'))
    atomic_json(Path(request['output']), output)
    write_progress(progress, dict(phase='asr_decode', message='Fun-ASR 识别完成', done=duration, total=duration, unit='seconds'))


def main():
    block_network()
    from filelock import FileLock
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    # Reject unsupported source languages before loading weights.
    language_name(request.get('language'))
    with FileLock(Path(__file__).resolve().parents[1] / 'runtime/gpu-model.lock', timeout=86400):
        write_progress(request.get('progress'), dict(phase='asr_load', message='加载 Fun-ASR-Nano 模型'))
        models = load_models(request)
        if 'items' in request:
            for item in request['items']:
                try:
                    transcribe({**request, **item}, models)
                except Exception as exc:
                    atomic_json(Path(item['output']), dict(error=str(exc)[:1800]))
        else:
            transcribe(request, models)


if __name__ == '__main__':
    main()
