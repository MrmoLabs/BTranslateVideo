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
    tmp.write_text(json.dumps(dict(id=message['id'],error='') if r.get('empty_error') else dict(id=message['id'],ok=True)))
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
    for value in [-1,61]:
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
