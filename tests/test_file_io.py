import ctypes
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import threading

import pytest

from app import file_io


def sharing_error(code=5):
    error=PermissionError(errno.EACCES,'simulated Windows sharing conflict');error.winerror=code
    return error


def test_atomic_json_retries_sharing_conflicts_and_publishes_utf8_without_bom(tmp_path,monkeypatch):
    path=tmp_path/'进度.json';file_io.atomic_json(path,{'old':True})
    original=file_io.os.replace;calls=[];sleeps=[]
    def replace(source,target):
        calls.append(Path(source));assert json.loads(path.read_text('utf-8'))=={'old':True}
        if len(calls)<3:raise sharing_error(32)
        original(source,target)
    monkeypatch.setattr(file_io.os,'replace',replace);monkeypatch.setattr(file_io.time,'sleep',sleeps.append)
    file_io.atomic_json(path,{'内容':'日本語和中文'})
    assert json.loads(path.read_text('utf-8'))=={'内容':'日本語和中文'}
    assert not path.read_bytes().startswith(b'\xef\xbb\xbf')
    assert len(calls)==3 and len(sleeps)==2 and len(set(calls))==1
    assert not list(tmp_path.glob('*.tmp'))


def test_durable_write_does_not_hide_permanent_access_denial(tmp_path,monkeypatch):
    path=tmp_path/'project.json';file_io.atomic_json(path,{'saved':True});attempts=[]
    def denied(*_):attempts.append(True);raise sharing_error()
    monkeypatch.setattr(file_io.os,'replace',denied);monkeypatch.setattr(file_io.time,'sleep',lambda *_:None)
    with pytest.raises(PermissionError):file_io.atomic_json(path,{'saved':False})
    assert len(attempts)==len(file_io.RETRY_DELAYS)+1
    assert json.loads(path.read_text('utf-8'))=={'saved':True} and not list(tmp_path.glob('*.tmp'))


def test_progress_can_skip_blocked_refresh_and_publish_latest_value_next_time(tmp_path,monkeypatch,caplog):
    path=tmp_path/'progress.json';file_io.atomic_json(path,{'completed':1})
    original=file_io.os.replace;calls=[]
    def denied(*_):calls.append(True);raise sharing_error()
    with monkeypatch.context() as patch:
        patch.setattr(file_io.os,'replace',denied);patch.setattr(file_io.time,'sleep',lambda *_:None)
        for n in range(2,5):assert file_io.write_progress(path,{'completed':n}) is False
    assert len(calls)==3*(len(file_io.PROGRESS_RETRY_DELAYS)+1)
    assert json.loads(path.read_text('utf-8'))=={'completed':1}
    assert len(caplog.records)==1 and 'continuing processing' in caplog.text
    assert file_io.write_progress(path,{'completed':5}) is True
    assert json.loads(path.read_text('utf-8'))=={'completed':5}
    assert not list(tmp_path.glob('*.tmp'))


def test_nonsharing_storage_error_is_not_retried_or_suppressed_for_results(tmp_path,monkeypatch):
    calls=[]
    def full(*_):calls.append(True);raise OSError(errno.ENOSPC,'disk full')
    monkeypatch.setattr(file_io.os,'replace',full)
    monkeypatch.setattr(file_io.time,'sleep',lambda *_:pytest.fail('must not retry disk full'))
    with pytest.raises(OSError,match='disk full'):file_io.atomic_json(tmp_path/'result.json',{'ok':True})
    assert len(calls)==1 and not list(tmp_path.glob('*.tmp'))


def test_invalid_json_values_preserve_previous_saved_file(tmp_path):
    path=tmp_path/'settings.json';file_io.atomic_json(path,{'saved':True})
    with pytest.raises(ValueError):file_io.atomic_json(path,{'bad':float('nan')})
    with pytest.raises(ValueError):file_io.write_progress(path,{'bad':float('nan')})
    assert json.loads(path.read_text('utf-8'))=={'saved':True} and not list(tmp_path.glob('*.tmp'))


def test_concurrent_writers_never_share_temporary_files(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'state.json';original=file_io.os.replace;barrier=threading.Barrier(4);temporary=set();guard=threading.Lock()
    def replace(source,target):
        with guard:
            first=Path(source) not in temporary;temporary.add(Path(source))
        if first:barrier.wait(timeout=3)
        original(source,target)
    monkeypatch.setattr(file_io.os,'replace',replace)
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures=[workers.submit(file_io.atomic_json,path,{'writer':i}) for i in range(4)]
        for future in futures:future.result(timeout=5)
    assert len(set(temporary))==4 and json.loads(path.read_text('utf-8'))['writer'] in range(4)
    assert not list(tmp_path.glob('*.tmp'))


@contextmanager
def deny_delete(path):
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW;create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
    close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
    handle=create(str(path),0x80000000,0x1|0x2,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:yield
    finally:close(handle)


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing semantics')
def test_real_windows_read_handle_reproduces_original_failure_and_progress_recovers(tmp_path):
    path=tmp_path/'tts-progress.json';pending=tmp_path/'tts-progress.pending.json'
    file_io.atomic_json(path,{'completed':1});pending.write_text('{"completed":2}',encoding='utf-8')
    with deny_delete(path):
        with pytest.raises(PermissionError) as error:os.replace(pending,path)
        assert error.value.winerror in {5,32,33}
        assert file_io.write_progress(path,{'completed':2}) is False
        assert json.loads(path.read_text('utf-8'))['completed']==1
    assert file_io.write_progress(path,{'completed':3}) is True
    assert json.loads(path.read_text('utf-8'))['completed']==3


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing semantics')
def test_real_windows_durable_write_waits_for_short_reader(tmp_path):
    path=tmp_path/'result.json';file_io.atomic_json(path,{'old':True});locked=threading.Event();release=threading.Event()
    def reader():
        with deny_delete(path):locked.set();release.wait(2)
    thread=threading.Thread(target=reader);thread.start();assert locked.wait(2)
    timer=threading.Timer(.06,release.set);timer.start()
    try:file_io.atomic_json(path,{'ok':True})
    finally:release.set();thread.join(2);timer.cancel()
    assert json.loads(path.read_text('utf-8'))=={'ok':True}


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing semantics')
def test_synthesis_finishes_all_clips_and_durable_stats_while_progress_is_locked(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    from scripts import tts_batch as batch
    cuda=SimpleNamespace(mem_get_info=lambda:(8*1024**3,16*1024**3),memory_reserved=lambda:0,memory_allocated=lambda:0,empty_cache=lambda:None,synchronize=lambda:None,max_memory_allocated=lambda:0)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=cuda))
    model=SimpleNamespace(cache_spk_audio_prompt='same.wav',cache_spk_cond=True)
    rows=[dict(id=i,text='测试文本',reference='same.wav',output=str(tmp_path/f'{i}.wav')) for i in range(4)]
    def generate(_model,group):
        for row in group:Path(row['output']).write_bytes(b'completed audio')
    monkeypatch.setattr(batch,'native_batch',generate)
    progress=tmp_path/'progress.json';stats=tmp_path/'stats.json'
    file_io.atomic_json(progress,{'completed':0})
    with deny_delete(progress):
        result=batch.run_clips(model,rows,2,stats_path=stats,progress_path=progress)
        assert json.loads(progress.read_text('utf-8'))=={'completed':0}
    assert result['clips']==4 and json.loads(stats.read_text('utf-8'))['clips']==4
    assert all(Path(row['output']).read_bytes()==b'completed audio' for row in rows)
    assert batch.report(progress,{'completed':4}) is True
    assert json.loads(progress.read_text('utf-8'))['completed']==4
