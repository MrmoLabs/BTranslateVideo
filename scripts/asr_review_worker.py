"""Offline batch re-transcription: one Whisper model, independent clip results."""
import json
import sys
from pathlib import Path
from model_worker import asr,block_network,write

def transcribe_items(request,model):
    for item in request['items']:
        try:asr({**request,**item},model=model)
        except Exception as exc:write(item['output'],dict(error=str(exc)[:1800]))

if __name__=='__main__':
    block_network()
    from filelock import FileLock
    from faster_whisper import WhisperModel
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    with FileLock(Path(__file__).resolve().parents[1]/'runtime/gpu-model.lock',timeout=86400):
        model=WhisperModel(request['model'],device=request['device'],compute_type=request['compute_type'],local_files_only=True)
        transcribe_items(request,model)
