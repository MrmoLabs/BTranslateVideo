import json
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from fastapi.testclient import TestClient
from app.config import Settings,atomic_json
from app.media import Cancelled,ffmpeg
from app.pipeline import Pipeline
from app.progress import advance,finish,new_progress,tts_detail
from app.server import create_app
from app.store import Store


def test_unknown_work_and_phase_changes_never_invent_progress_or_eta():
    p=new_progress('all',at=100)
    p=advance(p,phase='asr_load',step='analyze',at=110)
    assert p['done'] is None and p['total'] is None and 'eta_seconds' not in p
    assert p['completed_steps']==[] and p['started_at']==110
    p=advance(p,phase='asr_decode',done=2,total=10,unit='seconds',at=120)
    assert p['done']==2 and p['total']==10
    p=advance(p,step='translate',phase='translation_model',at=130)
    assert p['done'] is None and p['completed_steps']==['analyze'] and not p['samples']
    assert tts_detail({'phase':'加载模型','completed':0,'total':10})['done'] is None


def test_phase_eta_uses_only_recent_measured_samples_and_ignores_idle_polls():
    p=advance(new_progress('dub',at=0),phase='model_loading',at=100)
    p=advance(p,phase='synthesis',done=0,total=10,unit='segments',at=1000)
    p=advance(p,done=1,total=10,unit='segments',at=1005)
    assert 'eta_seconds' not in p
    p=advance(p,done=2,total=10,unit='segments',at=1010)
    assert p['eta_seconds']==48
    unchanged=advance(p,done=2,total=10,unit='segments',at=1100)
    assert unchanged==p and unchanged['updated_at']==1010
    changed=advance(p,phase='align',done=0,total=10,unit='segments',at=1120)
    assert 'eta_seconds' not in changed and len(changed['samples'])==1


def test_cancelled_failed_interrupted_tasks_preserve_completed_work():
    queued=advance(new_progress('dub',at=10),status='cancelling',at=15)
    assert queued['started_at'] is None
    p=advance(new_progress('dub',at=10),phase='synthesis',done=3,total=8,unit='segments',at=15)
    for status in ['cancelled','failed','interrupted']:
        ended=finish(p,status,message='Stopped',at=20)
        assert ended['done']==3 and ended['total']==8 and ended['finished_at']==20
        assert ended['completed_steps']==[] and advance(ended,done=8,total=8,at=30)==ended
    assert finish(p,'done',at=20)['completed_steps']==['dub']


def test_full_video_workflow_steps_do_not_skip_or_overwrite_edits(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');project=store.create('progress test','en','course');pid=project['id']
    store.update(pid,processing=new_progress('all'),segments=[dict(id=1,start=0,end=1,text='Keep me',zh='Keep me',speaker='speaker_1')])
    pipeline=Pipeline(store);seen=[]
    for stage in ['analyze','translate','dub','export']:
        def work(identifier,settings,cancel,name=stage):
            p=store.read(pid)['processing'];seen.append((name,p['step'],list(p['completed_steps'])))
            pipeline.progress(pid,name,50,'Measured work',dict(phase=name+'-work',done=1,total=2,unit='segments'))
        monkeypatch.setattr(pipeline,stage,work)
    try:pipeline.execute(pid,'all',Settings(),threading.Event())
    finally:pipeline.close()
    assert [x[1] for x in seen]==['analyze','translate','dub','export']
    assert seen[0][2]==[] and seen[-1][2]==['analyze','translate','dub']
    result=store.read(pid);assert result['status']=='done' and result['processing']['status']=='done'
    assert result['segments'][0]['text']=='Keep me' and result['edit_revision']==1


def test_failure_and_recovery_freeze_progress_without_marking_full_completion(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');pid=store.create('failure','en','course')['id']
    pipeline=Pipeline(store);store.update(pid,processing=new_progress('dub'))
    def fail(identifier,*args):
        pipeline.progress(pid,'dub',20,'Working',dict(phase='synthesis',done=2,total=10,unit='segments'))
        raise RuntimeError('Expected failure')
    monkeypatch.setattr(pipeline,'dub',fail)
    try:pipeline.execute(pid,'dub',Settings(),threading.Event())
    finally:pipeline.close()
    p=store.read(pid);assert p['status']=='failed' and p['processing']['done']==2 and p['progress']!=100
    active=advance(new_progress('analyze',at=100),phase='asr_decode',done=4,total=10,unit='seconds',at=120)
    store.update(pid,status='running',processing=active);store.recover()
    recovered=store.read(pid)['processing'];assert recovered['status']=='interrupted' and recovered['finished_at']==120


def test_global_tasks_queue_positions_logs_and_book_metadata(tmp_path):
    app=create_app(tmp_path/'projects');pipeline=app.state.pipeline;store=app.state.store
    with TestClient(app) as client:
        one=store.create('<b>literal title</b>','en','course');two=store.create('waiting','en','course')
        store.update(one['id'],status='running',processing=advance(new_progress('dub'),phase='synthesis',done=2,total=10,unit='segments'))
        store.update(two['id'],status='queued',processing=new_progress('translate'))
        book=app.state.books.create('Book');job=dict(status='queued',kind='generate',completed=0,total=7,processing=new_progress('audiobook'))
        app.state.books.update_book(book['id'],job=job)
        for key in [one['id'],two['id'],'audiobook:'+book['id']]:pipeline.events[key]=threading.Event()
        result=client.get('/api/tasks').json();assert result['active']==3
        indexed={item['id']:item for item in result['items']}
        assert indexed[two['id']]['queue_ahead']==1 and indexed[book['id']]['overall_total']==7
        assert indexed[one['id']]['title']=='<b>literal title</b>'
        (store.folder(one['id'])/'process.log').write_text('x'*30000+'END',encoding='utf-8')
        log=client.get('/api/tasks/video/'+one['id']+'/logs').json()
        assert log['truncated'] and len(log['content'])<=15000 and log['content'].endswith('END')
        assert client.get('/api/tasks/video/invalid/logs').status_code==400
        assert client.get('/api/tasks/other/invalid/logs').status_code==400


def test_ffmpeg_reports_real_output_time_and_removes_progress_files(tmp_path):
    values=[]
    ffmpeg(Settings(),['-f','lavfi','-i','sine=frequency=220:duration=2','-c:a','pcm_s16le',str(tmp_path/'tone.wav')],tmp_path/'media.log',threading.Event(),duration=2,on_progress=lambda done,total:values.append((done,total)))
    assert values and values[-1][0]>=1.9 and all(0<=done<=total==2 for done,total in values)
    assert not list(tmp_path.glob('*.progress'))


def test_asr_worker_reports_loading_and_real_transcription_timestamps(tmp_path,monkeypatch):
    from scripts import model_worker
    audio=tmp_path/'input.wav'
    with wave.open(str(audio),'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(b'\0\0'*8000)
    segment=SimpleNamespace(text='Hello',start=0,end=.4,words=[SimpleNamespace(word='Hello',start=0,end=.4)])
    model=SimpleNamespace(transcribe=lambda *args,**kwargs:(iter([segment]),SimpleNamespace(duration=.5)))
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=lambda *args,**kwargs:model))
    events=[];original=model_worker.write_progress
    def write(path,value):
        if str(path).endswith('progress.json'):events.append(value)
        original(path,value)
    monkeypatch.setattr(model_worker,'write_progress',write)
    model_worker.asr(dict(model='fixture',device='cpu',compute_type='int8',audio=str(audio),language='en',progress=str(tmp_path/'progress.json'),output=str(tmp_path/'out.json')))
    assert events[0]['phase']=='asr_load' and events[-1]['done']==events[-1]['total']==.5
    assert any(event.get('done')==.4 for event in events)


def test_queued_cancellation_finishes_immediately_without_waiting_for_running_job(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');pipeline=app.state.pipeline;store=app.state.store
    ready=threading.Event();release=threading.Event()
    def blocked(*args):ready.set();release.wait(5)
    monkeypatch.setattr(pipeline,'analyze',blocked)
    with TestClient(app) as client:
        first=store.create('running','en','course');second=store.create('queued','en','course')
        pipeline.submit(first['id'],'analyze',Settings());assert ready.wait(2)
        try:
            pipeline.submit(second['id'],'analyze',Settings())
            assert client.post('/api/projects/'+second['id']+'/cancel').status_code==200
            result=store.read(second['id'])
            assert result['status']==result['processing']['status']=='cancelled'
            assert result['processing']['started_at'] is None and second['id'] not in pipeline.events
            assert client.get('/api/tasks').json()['active']==1
        finally:release.set()


@pytest.mark.parametrize('status,expected',[('idle','cancelled'),('done','done'),('failed','failed')])
def test_cancel_clears_unowned_active_progress_and_preserves_content(tmp_path,status,expected):
    store=Store(tmp_path/'projects');pid=store.create('orphan','zh','course')['id']
    rows=[dict(id=1,start=0,end=1,text='保留字幕',zh='',speaker='speaker_1')]
    progress=advance(new_progress('analyze'),phase='analyze',message='识别已完成')
    store.update(pid,status=status,error='existing error' if status=='failed' else '',segments=rows,processing=progress)
    path=store.folder(pid)/'project.json';before=path.read_bytes()
    pipeline=Pipeline(store)
    try:
        pipeline.cancel(pid)
        result=store.read(pid)
        assert result['status']==result['processing']['status']==expected
        assert result['processing']['finished_at'] is not None
        assert result['segments']==rows and path.read_bytes()==before
        assert not pipeline.events and not pipeline.futures
        if status=='failed':assert result['error']=='existing error'
        pipeline.cancel(pid)
        assert store.read(pid)==result
    finally:pipeline.close()


@pytest.mark.parametrize('status,expected',[('idle','interrupted'),('done','done'),('failed','failed')])
def test_recovery_closes_active_progress_even_when_project_is_not_active(tmp_path,status,expected):
    store=Store(tmp_path/'projects');pid=store.create('orphan','zh','course')['id']
    progress=advance(new_progress('analyze',at=100),phase='asr_decode',done=4,total=4,unit='seconds',at=120)
    store.update(pid,status=status,processing=progress,error='keep' if status=='failed' else '')
    store.recover()
    result=store.read(pid)
    assert result['status']==result['processing']['status']==expected
    assert result['processing']['finished_at']==120
    if status=='failed':assert result['error']=='keep'
    if status=='idle':assert result['processing']['completed_steps']==[]
    store.recover()
    assert store.read(pid)==result


def test_speech_estimate_excludes_first_clip_warmup():
    p=new_progress('dub',at=0)
    for at,count in [(10,0),(100,1),(105,2),(110,3)]:
        p=advance(p,phase='tts_generate',done=count,total=10,unit='segments',at=at)
    assert p['samples'][0]==[100,1] and p['eta_seconds']==42
    assert tts_detail(dict(phase='准备参考声音',actual_batch=1,completed=0,total=10))['phase']=='tts_generate'
    assert tts_detail(dict(phase='片段已保存',actual_batch=1,completed=3,total=10))['phase']=='tts_generate'
