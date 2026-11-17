import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings, atomic_json
from app.media import Cancelled
from app.tts_session import TTSSession
from scripts.tts_service import refresh_reference_cache


@pytest.fixture
def session(tmp_path):
    helper=tmp_path/'worker.py'
    helper.write_text('''import json,os,sys,time
from pathlib import Path
count=0
for line in sys.stdin:
    message=json.loads(line);r=json.loads(Path(message['request']).read_text())
    if r.get('crash'):sys.exit(2)
    time.sleep(r.get('delay',0))
    count+=1
    output=Path(message['result']);tmp=output.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(id=message['id'],error='') if r.get('empty_error') else dict(id=message['id'],ok=True,model_loaded=not r.get('no_load_ack'),model_load_seconds=.25)))
    os.replace(tmp,output)
''',encoding='utf-8')
    starts=[]
    def popen(args,**kwargs):
        starts.append(args)
        return subprocess.Popen([sys.executable,str(helper)],**kwargs)
    value=TTSSession(tmp_path,popen=popen)
    value.starts=starts
    yield value
    value.close()


def request(tmp_path,**values):
    path=tmp_path/'request.json'
    atomic_json(path,{**dict(index_root=str(tmp_path/'index'),model=str(tmp_path/'model'),version='2.5',clips=[]),**values})
    return path


def run(session,tmp_path,cancel=None,idle=10):
    session.run(tmp_path/'request.json',sys.executable,'unused-worker',tmp_path/'log.txt',
                cancel or threading.Event(),dict(os.environ),idle)


def test_consecutive_jobs_reuse_process_and_manual_release_restarts_it(session,tmp_path):
    request(tmp_path)
    run(session,tmp_path)
    pid=session.status()['pid']
    run(session,tmp_path)
    assert session.status()['pid']==pid and len(session.starts)==1
    assert session.status()['reason']=='复用已加载模型'
    session.release()
    assert not session.status()['alive']
    run(session,tmp_path)
    assert len(session.starts)==2


def test_model_file_change_reloads_session_and_idle_expiry_releases_it(session,tmp_path):
    request(tmp_path)
    model=tmp_path/'model';model.mkdir();weight=model/'gpt.pth';weight.write_bytes(b'first')
    run(session,tmp_path)
    weight.write_bytes(b'new model weights')
    run(session,tmp_path,idle=.05)
    assert len(session.starts)==2
    deadline=time.monotonic()+3
    while session.status()['alive'] and time.monotonic()<deadline:time.sleep(.02)
    assert not session.status()['alive']


def test_cancel_and_crash_release_worker_and_next_job_recovers(session,tmp_path):
    request(tmp_path,delay=5)
    cancel=threading.Event();timer=threading.Timer(.3,cancel.set);timer.start()
    with pytest.raises(Cancelled):run(session,tmp_path,cancel)
    timer.cancel()
    assert not session.status()['alive']
    request(tmp_path,crash=True)
    with pytest.raises(RuntimeError,match='进程已退出'):run(session,tmp_path)
    request(tmp_path)
    run(session,tmp_path)
    assert session.status()['alive'] and len(session.starts)==3


def test_busy_release_is_rejected_and_close_prevents_restart(session,tmp_path):
    ready=threading.Event();finish=threading.Event()
    def hold():
        with session.guard:ready.set();finish.wait(3)
    thread=threading.Thread(target=hold);thread.start();ready.wait(3)
    try:
        with pytest.raises(RuntimeError,match='正在工作'):session.release(blocking=False)
    finally:finish.set();thread.join()
    session.close();request(tmp_path)
    with pytest.raises(RuntimeError,match='会话已关闭'):run(session,tmp_path)


def test_replacing_reference_at_same_path_invalidates_cached_condition(tmp_path):
    ref=tmp_path/'voice.wav';ref.write_bytes(b'old voice')
    model=SimpleNamespace(cache_spk_audio_prompt=str(ref),cache_spk_cond=object(),cache_emo_audio_prompt=str(ref),cache_emo_cond=object())
    signatures={};clips=[dict(reference=str(ref))]
    refresh_reference_cache(model,clips,signatures)
    assert model.cache_spk_cond is not None
    ref.write_bytes(b'new voice')
    refresh_reference_cache(model,clips,signatures)
    assert model.cache_spk_cond is None and model.cache_emo_cond is None


def test_warm_policy_bounds_and_api_release(tmp_path):
    from fastapi.testclient import TestClient
    from app.server import create_app
    for value in [-1,1441]:
        with pytest.raises(ValueError):Settings(tts_idle_minutes=value)
    with TestClient(create_app(tmp_path/'projects')) as client:
        assert client.get('/api/tts-session').json()['alive'] is False
        assert client.post('/api/tts-session/release').status_code==200
        assert client.post('/api/tts-session/release',headers={'Origin':'https://evil.example'}).status_code==403


def test_status_tolerates_idle_release_during_poll(tmp_path):
    session=TTSSession(tmp_path)
    def poll():
        session.process=None
        return None
    session.process=SimpleNamespace(pid=123,poll=poll)
    assert session.status()["pid"]==123
    session.close()


def test_early_idle_callback_retries_and_stale_callback_preserves_new_session(session, tmp_path, monkeypatch):
    timers = []

    class ManualTimer:
        def __init__(self, interval, function, args=()):
            self.interval, self.function, self.args = interval, function, args
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def fire(self):
            self.function(*self.args)

    monkeypatch.setattr('app.tts_session.threading.Timer', ManualTimer)
    now = [100.0]
    session.clock = lambda: now[0]
    request(tmp_path)
    run(session, tmp_path, idle=10)
    now[0] = 109.999
    timers[-1].fire()
    assert session.status()['alive'] and len(timers) == 2
    assert timers[-1].interval == pytest.approx(.01)
    previous_timer = timers[-1]

    run(session, tmp_path, idle=20)
    current_timer = timers[-1]
    now[0] = 110.0
    previous_timer.fire()
    assert session.status()['alive'] and len(timers) == 3
    assert session.timer is current_timer

    now[0] = session.deadline
    current_timer.fire()
    assert not session.status()['alive']
    assert session.status()['reason'] == '空闲保温已到期，自动释放显存'


def test_code_timestamp_changes_preserve_model_but_content_changes_reload(session, tmp_path):
    import os
    request(tmp_path)
    code=tmp_path/'index/indextts/infer_v2_5.py'
    code.parent.mkdir(parents=True)
    code.write_bytes(b'first source')
    run(session,tmp_path)
    original=code.stat()
    os.utime(code,ns=(original.st_atime_ns,original.st_mtime_ns+1_000_000_000))
    run(session,tmp_path)
    assert len(session.starts)==1 and session.run_info['model_reused']
    timestamp=code.stat()
    code.write_bytes(b'other source')
    os.utime(code,ns=(timestamp.st_atime_ns,timestamp.st_mtime_ns))
    run(session,tmp_path)
    assert len(session.starts)==2 and not session.run_info['model_reused']
    assert session.run_info['model_load_reason']=='模型或运行环境发生变化'


def test_emotion_and_text_changes_reuse_model_and_publish_current_phase(session,tmp_path):
    request(tmp_path,clips=[dict(text='first',emotion='happy')])
    run(session,tmp_path)
    request(tmp_path,clips=[dict(text='second',emotion='sad')])
    states=[]
    session.run(tmp_path/'request.json',sys.executable,'unused-worker',tmp_path/'log.txt',
                threading.Event(),dict(os.environ),10,on_tick=lambda:states.append(session.status()['phase']))
    assert len(session.starts)==1 and session.run_info['model_reused']
    assert states[0]=='复用已加载模型'


def test_empty_worker_error_is_a_failure_and_releases_the_model(session,tmp_path):
    request(tmp_path,empty_error=True)
    with pytest.raises(RuntimeError,match='执行失败'):run(session,tmp_path)
    assert not session.status()['alive']


def test_explicit_preload_is_reused_even_in_after_task_mode(session,tmp_path):
    request(tmp_path,operation='load')
    run(session,tmp_path,idle=None)
    status=session.status();pid=status['pid']
    assert status['loaded'] and status['keep_loaded'] and session.timer is None
    assert session.run_info['model_load_seconds']==.25
    request(tmp_path,clips=[dict(text='A real request after preload')])
    run(session,tmp_path,idle=0)
    assert len(session.starts)==1 and session.run_info['model_reused']
    assert not session.status()['alive'] and not session.status()['loaded']


def test_loaded_ack_and_live_retention_changes(session,tmp_path):
    request(tmp_path,operation='load',no_load_ack=True)
    with pytest.raises(RuntimeError,match='未确认加载'):run(session,tmp_path,idle=None)
    assert not session.status()['loaded']
    request(tmp_path,operation='load');run(session,tmp_path,idle=None)
    assert session.configure_retention(.05)
    deadline=time.monotonic()+3
    while session.status()['alive'] and time.monotonic()<deadline:time.sleep(.02)
    assert not session.status()['alive']
    run(session,tmp_path,idle=10)
    assert session.configure_retention(None) and session.timer is None
    assert session.status()['keep_loaded']
    assert session.configure_retention(0) and not session.status()['alive']


def test_preload_cancellation_and_policy_save_never_interrupt_active_synthesis(session,tmp_path):
    request(tmp_path,operation='load',delay=5)
    cancel=threading.Event();errors=[]
    def load():
        try:run(session,tmp_path,cancel,idle=None)
        except Cancelled as exc:errors.append(exc)
    thread=threading.Thread(target=load);thread.start()
    deadline=time.monotonic()+3
    while not session.running and time.monotonic()<deadline:time.sleep(.01)
    assert session.running and not session.status()['loaded']
    assert session.configure_retention(0) is False
    with pytest.raises(RuntimeError):session.release(blocking=False)
    cancel.set();thread.join(3)
    assert not thread.is_alive() and errors and not session.status()['alive']


def test_stdio_preload_loads_weights_without_synthesizing(tmp_path,monkeypatch):
    import io
    from scripts import tts_service
    calls=[]
    def load_model(request):calls.append('load');return object(),.5
    def synthesize(*args,**kwargs):calls.append('synthesis')
    fake=SimpleNamespace(load_tts=load_model,tts=synthesize,write=lambda path,value:atomic_json(Path(path),value))
    monkeypatch.setitem(sys.modules,'model_worker',fake)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace())
    class Lock:
        def __init__(self,*args):pass
        def acquire(self,**kwargs):pass
        def release(self):pass
    monkeypatch.setattr('filelock.FileLock',Lock)
    source=request(tmp_path,operation='load')
    commands=[dict(id=str(i),request=str(source),result=str(tmp_path/f'result-{i}.json')) for i in range(2)]
    monkeypatch.setattr(sys,'stdin',io.StringIO(''.join(json.dumps(c)+'\n' for c in commands)))
    tts_service.serve(tmp_path)
    assert calls==['load']
    assert all(json.loads((tmp_path/f'result-{i}.json').read_text())['model_loaded'] for i in range(2))
    assert json.loads((tmp_path/'result-1.json').read_text())['model_load_seconds']==0


def test_manual_lifecycle_api_retention_and_busy_guards(session,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app.server import create_app
    path=tmp_path/'settings.json'
    current=Settings(tts_keep_loaded=True,translation_name='keep-other-settings')
    atomic_json(path,current.model_dump())
    monkeypatch.setattr('app.server.SETTINGS',path)
    monkeypatch.setattr('app.server.load_settings',lambda:Settings.model_validate_json(path.read_text(encoding='utf-8')))
    monkeypatch.setattr('app.pipeline.ROOT',tmp_path)
    monkeypatch.setattr('app.pipeline.index_python',lambda _:Path(sys.executable))
    monkeypatch.setattr('app.model_backends.model_readiness',lambda _:dict(tts_ready=True))
    app=create_app(tmp_path/'projects');pipeline=app.state.pipeline
    pipeline.tts.close();pipeline.tts=session
    with TestClient(app) as client:
        assert client.post('/api/tts-session/load',headers={'Origin':'https://evil.example'}).status_code==403
        response=client.post('/api/tts-session/load');assert response.status_code==200
        assert response.json()['control']['status'] in {'queued','loading','ready'}
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=client.get('/api/tts-session').json()
            if state['control']['status']=='ready':break
            time.sleep(.02)
        assert state['loaded'] and state['keep_loaded'] and len(session.starts)==1,state
        response=client.put('/api/tts-session/retention',json={'tts_idle_minutes':1.5,'tts_keep_loaded':False})
        assert response.status_code==200 and response.json()['applied']
        assert 89<=response.json()['session']['idle_seconds']<=90
        saved=json.loads(path.read_text());assert saved['translation_name']=='keep-other-settings' and saved['tts_idle_minutes']==1.5
        assert not path.read_bytes().startswith(b'\xef\xbb\xbf')
        for minutes in [-.1,1440.1]:
            assert client.put('/api/tts-session/retention',json={'tts_idle_minutes':minutes}).status_code==422
        pipeline.events['other-project']=threading.Event()
        try:
            assert client.post('/api/tts-session/load').status_code==409
            assert client.post('/api/tts-session/release').status_code==409
            client.post('/api/tts-session/cancel-load')
            assert not pipeline.events['other-project'].is_set()
        finally:pipeline.events.pop('other-project')
        assert client.post('/api/tts-session/release').json()['loaded'] is False
        saved.update(tts_provider='openai_speech',tts_url='http://localhost:9000/v1',tts_name='test')
        atomic_json(path,saved)
        assert client.post('/api/tts-session/load').status_code==409
        assert client.get('/api/tts-session/logs').status_code==200


def test_pipeline_consumes_preload_before_unloading_after_task(session,tmp_path,monkeypatch):
    from app.pipeline import Pipeline
    from app.store import Store
    request(tmp_path,operation='load');run(session,tmp_path,idle=None)
    settings=Settings(index_root=str(tmp_path/'index'),index_model=str(tmp_path/'model'),tts_idle_minutes=0)
    monkeypatch.setattr('app.pipeline.index_python',lambda _:Path(sys.executable))
    def unexpected(*args,**kwargs):raise AssertionError('Preloaded model must not start a one-shot worker')
    monkeypatch.setattr('app.pipeline.run',unexpected)
    store=Store(tmp_path/'projects');project=store.create('preload reuse','en','course')
    folder=store.folder(project['id']);(folder/'cache').mkdir()
    pipeline=Pipeline(store);pipeline.tts.close();pipeline.tts=session
    try:
        pipeline.synthesize(project['id'],[dict(text='Hello',reference='',output=str(folder/'cache/clip.wav'))],1,settings,threading.Event())
        assert len(session.starts)==1 and session.run_info['model_reused']
        assert not session.status()['loaded']
    finally:pipeline.close()
