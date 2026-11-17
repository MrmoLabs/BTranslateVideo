"""Opt-in real-backend acceptance; reports quality observations separately from IO checks.

Never downloads models, modifies settings, or uses a private project/voice.
"""
from __future__ import annotations
import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings,load_settings,atomic_json
from app.pipeline import Pipeline,offline_env
from app.store import Store
from app.model_backends import transcribe
from app.translation import local_translator,translate_batch
from app.translation_checks import numbers
from app.media import wav_duration


def edit_rate(expected,actual,language):
    def tokens(text):
        text=unicodedata.normalize('NFKC',text).casefold()
        return re.findall(r'\w+',text) if language=='en' else [c for c in text if c.isalnum()]
    left,right=tokens(expected),tokens(actual);row=list(range(len(right)+1))
    for i,a in enumerate(left,1):
        next_row=[i]
        for j,b in enumerate(right,1):next_row.append(min(next_row[-1]+1,row[j]+1,row[j-1]+(a!=b)))
        row=next_row
    return dict(metric='WER' if language=='en' else 'CER',rate=row[-1]/max(1,len(left)),reference_units=len(left))


class DeviceMemory:
    """Whole-device readings, including unrelated workloads; never model-allocation claims."""
    def __enter__(self):
        self.stop=threading.Event();self.samples=[]
        def poll():
            while not self.stop.is_set():
                try:
                    result=subprocess.run(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    if result.returncode==0:self.samples.append({index.strip():int(used.strip()) for index,used in (line.split(',') for line in result.stdout.splitlines())})
                except (OSError,ValueError,subprocess.TimeoutExpired):pass
                self.stop.wait(.75)
        self.worker=threading.Thread(target=poll,daemon=True);self.worker.start();return self
    def __exit__(self,*args):self.stop.set();self.worker.join(4)
    def report(self):
        return dict(scope='whole-device MiB; includes other applications; sampling may miss short peaks',samples=len(self.samples),
                    baseline=self.samples[0] if self.samples else None,
                    peak={index:max(sample.get(index,0) for sample in self.samples) for index in self.samples[0]} if self.samples else None)


def ensure_app_idle():
    import httpx
    try:
        with httpx.Client(trust_env=False,timeout=3) as client:
            tasks=client.get('http://127.0.0.1:7861/api/tasks');tasks.raise_for_status()
            tts=client.get('http://127.0.0.1:7861/api/tts-session');tts.raise_for_status()
            value=tasks.json();items=value if isinstance(value,list) else value.get('items',[])
            if (isinstance(value,dict) and value.get('active',0)) or any((item.get('processing') or item).get('status') in {'queued','running','cancelling'} for item in items) or tts.json().get('alive'):
                raise RuntimeError('Main application has active tasks or a resident TTS model. Finish tasks and unload it in the UI before acceptance.')
    except httpx.ConnectError:pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stages',default='asr,translation,tts',help='comma-separated asr,translation,tts')
    parser.add_argument('--settings',type=Path,help='explicit local Settings JSON; otherwise current local settings')
    parser.add_argument('--fixtures',type=Path,default=ROOT/'runtime/backend-fixtures',help='authored WAV files named en-course.wav / ja-dialogue.wav')
    parser.add_argument('--prepare-windows-speech',action='store_true',help='generate authored audio using installed Windows system voices, without downloads')
    args=parser.parse_args();stages=args.stages.split(',')
    if set(stages)-{'asr','translation','tts'}:parser.error('unknown stage')
    settings=Settings.model_validate_json(args.settings.read_text(encoding='utf-8')) if args.settings else load_settings()
    ensure_app_idle()
    cases=json.loads((ROOT/'scripts/fixtures/backend_cases.json').read_text(encoding='utf-8'))
    if args.prepare_windows_speech:
        subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/prepare_backend_fixture.ps1'),'-OutputDirectory',str(args.fixtures.resolve())],check=True)
    base=ROOT/'runtime/backend-checks'/time.strftime('%Y%m%d-%H%M%S');base.mkdir(parents=True,exist_ok=False)
    report=dict(schema=1,python=platform.python_version(),platform=platform.platform(),stages=[],
                backends={key:getattr(settings,key+'_provider') for key in ('asr','translation','tts')},
                models=dict(asr=settings.asr_model if settings.asr_provider=='faster_whisper' else settings.asr_name,translation=settings.translation_model if not settings.translation_url else settings.translation_name,tts=settings.index_model if settings.tts_provider=='indextts' else settings.tts_name),
                quality_notice='Structural success and numeric checks do not prove translation meaning, speaker similarity or naturalness. Listen/read with the authored reference. Synthetic voices are a reproducibility fixture, not real-world video quality coverage.')
    cancel=threading.Event();log=base/'process.log'
    def record(stage,run):
        print('START',stage,flush=True);started=time.perf_counter()
        with DeviceMemory() as memory:
            try:value=run();status='completed'
            except Exception as exc:value=dict(error=str(exc));status='failed'
        item=dict(stage=stage,status=status,seconds=round(time.perf_counter()-started,3),gpu=memory.report(),**value)
        report['stages'].append(item);atomic_json(base/'report.json',report)
        print('FINISH',stage,status,item['seconds'],'seconds',flush=True)
    if 'asr' in stages:
        for case in cases['cases']:
            source=args.fixtures/(case['id']+'.wav')
            if not source.is_file():
                report['stages'].append(dict(stage='asr:'+case['id'],status='skipped',reason='No authored audio fixture. Install the matching Windows system voice or supply an authorized WAV.'));continue
            def asr(case=case,source=source):
                duration=wav_duration(source);output=base/(case['id']+'-asr.json')
                transcribe(settings,source,output,case['language'],'course',log,cancel,offline_env())
                rows=json.loads(output.read_text(encoding='utf-8'));text=' '.join(row['text'] for row in rows)
                return dict(reference=case['source'],recognized=text,quality=edit_rate(case['source'],text,case['language']),audio_seconds=duration,structure=dict(nonempty=bool(rows),word_timestamps=all(row.get('words') for row in rows)))
            record('asr:'+case['id'],asr)
    if 'translation' in stages:
        def translation():
            observations=[]
            with local_translator(settings,log,cancel) as url:
                for case in cases['cases']:
                    rows=[dict(id=1,text=case['source'],zh='',start=0,end=20,speaker='speaker_1')]
                    checks={};started=time.perf_counter()
                    result=translate_batch(url,settings,rows,[],'course',cancel,cache=base/case['id'],topic=dict(title='Authored backend acceptance',language=case['language'],target_language='zh',mode='course'),on_checks=checks.update)
                    text=result[1];observations.append(dict(id=case['id'],source=case['source'],translation=text,seconds=round(time.perf_counter()-started,3),numeric_invariants=all(numbers(text,written=True)[number]>0 for number in case['numbers']),no_kana=not bool(re.search(r'[\u3040-\u30ff]',text)),checks=checks,manual_review=case['review']))
            return dict(observations=observations)
        record('translation',translation)
    if 'tts' in stages:
        def tts():
            reference=args.fixtures/'en-course.wav'
            if not reference.is_file():raise ValueError('TTS reference fixture missing; prepare Windows speech or supply authored en-course.wav')
            store=Store(base/'projects');project=store.create('Independent backend acceptance','en','course');identifier=project['id'];folder=store.folder(identifier);(folder/'cache').mkdir();shutil.copyfile(reference,folder/'reference.wav')
            pipeline=Pipeline(store);observations=[]
            try:
                # Our isolated worker stays warm only between these two requests.
                local=settings.model_copy(update=dict(tts_idle_minutes=2,tts_keep_loaded=False))
                for index,case in enumerate(cases['tts'],1):
                    output=folder/'cache'/f'acceptance-{index}.wav';started=time.perf_counter()
                    pipeline.synthesize(identifier,[dict(id=index,text=case['text'],lang=case['language'].upper(),reference=str(folder/'reference.wav'),output=str(output))],1,local,cancel)
                    observations.append(dict(text=case['text'],audio=str(output.relative_to(base)),seconds=round(time.perf_counter()-started,3),audio_seconds=wav_duration(output),runtime=store.read_runtime(identifier).get('tts_runtime'),manual_review=case['review']))
                return dict(observations=observations)
            finally:pipeline.close()
        record('tts',tts)
    atomic_json(base/'report.json',report)
    print('REPORT',base/'report.json',flush=True)
    return int(any(item['status']=='failed' for item in report['stages']))


if __name__=='__main__':raise SystemExit(main())
