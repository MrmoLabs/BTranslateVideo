"""Offline ASR checks for benchmark audio; auxiliary evidence, not a listening verdict."""
import argparse,json,re,sys,subprocess
from difflib import SequenceMatcher
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import load_settings,resolve,executable,atomic_json
from filelock import FileLock

def normalized(text): return re.sub(r'[^\u3400-\u9fffA-Za-z0-9]','',text).lower()

def main(folder):
    from model_worker import block_network
    block_network()
    data=json.loads((folder/'benchmark.json').read_text(encoding='utf-8'));settings=load_settings();records=[]
    with FileLock(ROOT/'runtime/gpu-model.lock',timeout=3600):
        import numpy as np
        from faster_whisper import WhisperModel
        model=WhisperModel(str(resolve(settings.asr_model)),device='cuda',compute_type='float16',local_files_only=True)
        for label,profile in data['profiles'].items():
            for row in profile['outputs']:
                path=Path(row['output'])
                result=subprocess.run([executable(settings.ffmpeg),'-v','error','-i',str(path),'-f','s16le','-ac','1','-ar','16000','pipe:1'],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                audio=np.frombuffer(result.stdout,dtype='<i2').astype(np.float32)/32768
                pieces,_=model.transcribe(audio,language='zh',beam_size=5,condition_on_previous_text=False,vad_filter=False)
                text=''.join(p.text for p in pieces);ratio=SequenceMatcher(None,normalized(row['text']),normalized(text)).ratio()
                duration=len(audio)/16000
                item=dict(profile=label,id=row['id'],expected=row['text'],recognized=text,similarity=round(ratio,4),seconds=duration,non_silent=bool(np.max(np.abs(audio))>.01),passed=ratio>=.82 and duration>.2 and duration<30)
                records.append(item)
                atomic_json(folder/'content-verification.json',dict(note='ASR similarity checks omissions/repetition; it cannot prove speaker similarity, emotion or naturalness.',rows=records,all_passed=all(r['passed'] for r in records)))
                print(json.dumps(item,ensure_ascii=False),flush=True)
    print('ASR checks complete',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args();main(args.folder.resolve())
