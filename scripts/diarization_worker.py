"""Isolated Community-1 inference. Internet access is blocked before imports."""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import time
import wave

os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                  PYANNOTE_METRICS_ENABLED='0', DO_NOT_TRACK='1')
from model_worker import block_network, write


def process(request):
    import numpy as np
    import torch
    from filelock import FileLock
    from pyannote.audio import Pipeline
    from importlib.metadata import version
    device = request.get('device', 'auto')
    if device == 'auto':device = 'cuda' if torch.cuda.is_available() else 'cpu'
    if device == 'cuda' and not torch.cuda.is_available():raise ValueError('此独立环境无法使用 CUDA，请选择 CPU 或重新安装 CUDA 版 PyTorch')
    def report(phase, message, **detail):
        write(request['progress'], dict(phase=phase,message=message,**detail))
    lock = FileLock(Path(__file__).resolve().parents[1]/'runtime/gpu-model.lock', timeout=86400) if device == 'cuda' else nullcontext()
    report('diarization_wait', '等待 GPU' if device == 'cuda' else '准备 CPU 说话人识别')
    with lock:
        report('diarization_load', '从本机加载 Community-1')
        torch.set_num_threads(min(8,os.cpu_count() or 1))
        model = Path(request['model']).resolve()
        if not model.is_dir():raise ValueError('Community-1 模型目录不存在')
        # Community-1's local-directory loader resolves bundled segmentation,
        # embedding and PLDA paths. Never call its remote repository ID here.
        pipeline = Pipeline.from_pretrained(str(model))
        pipeline.to(torch.device(device))
        with wave.open(request['audio'],'rb') as stream:
            if (stream.getframerate(),stream.getnchannels(),stream.getsampwidth())!=(16000,1,2):raise ValueError('说话人识别需要 16kHz 单声道 PCM16 音频')
            audio = np.frombuffer(stream.readframes(stream.getnframes()),dtype='<i2').astype(np.float32)/32768.
        last = [None,0.]
        messages = {'segmentation':'检测人声与说话人切换','embeddings':'提取说话人声纹','clustering':'聚类并区分说话人','discrete_diarization':'对齐说话人时间轴','speaker_counting':'估算说话人数'}
        def hook(step_name, step_artifact, file=None, total=None, completed=None, **kwargs):
            at=time.monotonic()
            if step_name==last[0] and at-last[1]<.5 and (completed is None or completed!=total):return
            last[:]=[step_name,at]
            detail=dict(current=str(step_name))
            if isinstance(total,(int,float)) and total>0 and isinstance(completed,(int,float)):
                detail.update(done=completed,total=total,unit='chunks')
            report('diarization_'+str(step_name) if step_name in messages else 'diarization_compute',messages.get(step_name,'正在识别说话人'),**detail)
        options={key:request[key] for key in ['num_speakers','min_speakers','max_speakers'] if request.get(key) is not None}
        with torch.inference_mode():output=pipeline({'waveform':torch.from_numpy(audio).unsqueeze(0),'sample_rate':16000},hook=hook,**options)
        def turns(annotation):
            return [dict(start=float(turn.start),end=float(turn.end),speaker=str(speaker)) for turn,_,speaker in annotation.itertracks(yield_label=True)]
        # Exclusive diarization is for subtitle matching; regular diarization is
        # retained to warn about overlap and avoid contaminated voice samples.
        result=dict(turns=turns(output.speaker_diarization),exclusive=turns(output.exclusive_speaker_diarization),device=device,version=version('pyannote.audio'))
        write(request['output'],result)
        report('diarization_match','准备字幕角色分配')


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('request',type=Path,nargs='?');parser.add_argument('--check',action='store_true');args=parser.parse_args()
    block_network()
    if args.check:
        import torch
        from pyannote.audio import Pipeline
        from importlib.metadata import version
        print(json.dumps(dict(cuda_available=torch.cuda.is_available(),torch=torch.__version__,pyannote=version('pyannote.audio'))),flush=True)
        sys.exit(0)
    if not args.request:parser.error('request is required')
    request=json.loads(args.request.read_text(encoding='utf-8'))
    try:process(request)
    except Exception as exc:
        message=str(exc)
        if 'out of memory' in message.lower():message='说话人识别显存不足，请选择 CPU 或释放其他模型后重试；原角色保持不变'
        print(message,flush=True)
        raise
