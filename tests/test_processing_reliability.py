import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from app.audio_cache import reusable_voice_clip
from app.config import Settings,atomic_json
from app.pipeline import Pipeline,make_voice_clip
from app.store import Store
from app.role_lifecycle import reused_roles


def voice(path,frames=2400):
    with wave.open(str(path),'wb') as out:out.setparams((1,2,24000,0,'NONE',''));out.writeframes(b'\0\0'*frames)


@pytest.mark.parametrize('kind',['empty','not-wave','zero-frames','truncated'])
def test_broken_audio_is_quarantined_and_can_be_regenerated(tmp_path,kind):
    path=tmp_path/'tts-test.wav'
    if kind=='empty':path.touch()
    elif kind=='not-wave':path.write_bytes(b'corrupt')
    else:
        voice(path,0 if kind=='zero-frames' else 100)
        if kind=='truncated':path.write_bytes(path.read_bytes()[:-4])
    original=path.read_bytes();archived=[]
    assert reusable_voice_clip(path,lambda source,backup:archived.append(backup)) is False
    assert not path.exists() and len(archived)==1 and archived[0].read_bytes()==original
    assert reusable_voice_clip(path) is False
    voice(path);assert reusable_voice_clip(path) is True


def test_valid_audio_is_kept_and_access_errors_are_not_treated_as_corruption(tmp_path,monkeypatch):
    path=tmp_path/'voice.wav';voice(path);before=(path.read_bytes(),path.stat().st_mtime_ns)
    assert reusable_voice_clip(path) and (path.read_bytes(),path.stat().st_mtime_ns)==before
    def denied(*args,**kw):raise PermissionError('blocked')
    monkeypatch.setattr('app.audio_cache.wave.open',denied)
    with pytest.raises(PermissionError):reusable_voice_clip(path)
    assert not list(tmp_path.glob('*.bak')) and path.exists()


@pytest.mark.parametrize('action',['audition','dub'])
def test_video_audio_regenerates_corrupt_cached_clip_without_editing_script(tmp_path,monkeypatch,action):
    store=Store(tmp_path/'projects');p=store.create('cache recovery','en','course');id=p['id'];folder=store.folder(id);(folder/'cache').mkdir()
    ref=folder/'ref.wav';voice(ref);model=tmp_path/'model';model.mkdir()
    settings=Settings(index_model=str(model));row=dict(id=1,text='Source',zh='测试。',speaker='speaker_1',start=0,end=2)
    store.update(id,segments=[row],media={'duration':2},references={'speaker_1':'ref.wav'},audition_request={'id':'fixture','segment':row})
    monkeypatch.setattr('app.pipeline.tts_model_signature',lambda *_:[])
    monkeypatch.setattr('app.pipeline.index_python',lambda *_:Path(sys.executable))
    from app.pipeline import file_hash
    clip=make_voice_clip(row,folder,'ref.wav',settings,[],file_hash(ref));raw=Path(clip['output']);raw.write_bytes(b'broken cached audio')
    pipeline=Pipeline(store);calls=[]
    def synthesize(identifier,pending,total,*args):
        calls.extend(pending)
        for item in pending:voice(Path(item['output']))
    monkeypatch.setattr(pipeline,'synthesize',synthesize)
    try:
        getattr(pipeline,action)(id,settings,threading.Event())
        after=store.read(id);assert len(calls)==1 and reusable_voice_clip(raw)
        assert after['segments']==[row] and list((folder/'cache').glob('*.invalid-*.bak'))
        if action=='audition':assert (folder/after['audition']['file']).exists()
        else:assert (folder/after['outputs']['audio']).exists()
    finally:pipeline.close()


def test_synthesis_progress_limits_full_scans_and_forces_final_real_count(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');p=store.create('progress','en','course');id=p['id'];folder=store.folder(id);(folder/'cache').mkdir()
    old=folder/'cache/tts-progress.json';old.write_bytes(b'old snapshot, do not delete')
    rows=[dict(id=i,text='test',reference='',output=str(folder/'cache'/f'tts-{i}.wav')) for i in range(20)]
    pipeline=Pipeline(store);clock=[0.];scans=[];original=Path.is_file
    pending_paths={item['output'] for item in rows}
    def checked(path):
        if str(path) in pending_paths:scans.append(str(path))
        return original(path)
    monkeypatch.setattr(Path,'is_file',checked)
    monkeypatch.setattr('app.pipeline.time.monotonic',lambda:clock[0])
    request_paths=[]
    def run(request_path,*args,**kw):
        request=json.loads(Path(request_path).read_text('utf-8'));request_paths.append(request['progress'])
        for i in range(40):
            clock[0]=i*.15
            atomic_json(Path(request['progress']),{'phase':'声学生成','completed':0,'total':999,'seconds':i*.15})
            kw['on_tick']()
        for item in rows:voice(Path(item['output']))
    monkeypatch.setattr(pipeline.tts,'run',run)
    try:
        pipeline.synthesize(id,rows,len(rows),Settings(tts_idle_minutes=1),threading.Event())
        after=store.read(id)['tts_runtime']
        assert after['completed']==after['total']==20
        assert len(scans)<=12*len(rows),f'Unbounded cache scans: {len(scans)}'
        assert request_paths[0]!=str(old) and old.read_bytes()==b'old snapshot, do not delete'
        print('PROGRESS_BENCHMARK',{'callbacks':40,'old_full_scans':40,'new_full_scans':len(scans)//len(rows),'final_saved_clips':after['completed']})
    finally:pipeline.close()


def test_unlinked_manual_role_time_cannot_be_ignored_when_reusing_detected_role():
    project={'segments':[dict(start=0,end=3,speaker='old'),dict(start=3,end=30,speaker='manual')],
        'references':{},'roles':{'old':{'origin':'diarization'},'manual':{'origin':'manual'}}}
    assert reused_roles(project,[dict(start=0,end=30,speaker='A')],['A'])=={}


def test_merged_cluster_with_conflicting_reference_anchors_requires_review():
    project={'segments':[dict(start=0,end=10,speaker='one'),dict(start=10,end=20,speaker='two')],
        'references':{},'roles':{'one':{'origin':'diarization'},'two':{'origin':'diarization'}},
        'reference_origins':{'one':{'start':0,'duration':5},'two':{'start':10,'duration':5}}}
    assert reused_roles(project,[dict(start=0,end=20,speaker='merged')],['merged'])=={}
    assert reused_roles(project,[dict(start=0,end=10,speaker='A'),dict(start=10,end=20,speaker='B')],['A','B'])=={'A':'one','B':'two'}


def test_enqueue_write_failure_does_not_leave_ghost_busy_state(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');p=store.create('queue','en','course');pipeline=Pipeline(store)
    original=store.update
    def blocked(identifier,**values):
        if values.get('status')=='queued':raise PermissionError('project write blocked')
        return original(identifier,**values)
    monkeypatch.setattr(store,'update',blocked)
    try:
        with pytest.raises(PermissionError):pipeline.submit(p['id'],'preview',Settings())
        assert not pipeline.busy(p['id']) and p['id'] not in pipeline.futures
        assert store.read(p['id'])==p
    finally:pipeline.close()


def test_executor_rejection_is_terminal_and_releases_reserved_task(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');p=store.create('queue','en','course');pipeline=Pipeline(store)
    def rejected(*args,**kw):raise RuntimeError('executor unavailable')
    monkeypatch.setattr(pipeline.pool,'submit',rejected)
    try:
        with pytest.raises(ValueError,match='队列'):pipeline.submit(p['id'],'preview',Settings())
        assert not pipeline.busy(p['id']) and not pipeline.futures
        after=store.read(p['id']);assert after['status']=='failed' and after['processing']['status']=='failed'
        assert 'executor unavailable' in after['error']
    finally:pipeline.close()


def test_cache_directory_failure_finishes_task_instead_of_blocking_queue(tmp_path):
    import time
    store=Store(tmp_path/'projects');p=store.create('queue','en','course');pipeline=Pipeline(store)
    (store.folder(p['id'])/'cache').write_bytes(b'not a directory')
    try:
        pipeline.submit(p['id'],'preview',Settings());deadline=time.monotonic()+5
        while pipeline.busy(p['id']) and time.monotonic()<deadline:time.sleep(.01)
        assert not pipeline.busy(p['id']) and not pipeline.futures
        assert store.read(p['id'])['status']=='failed'
    finally:pipeline.close()


def test_preload_directory_failure_does_not_reserve_gpu_queue(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');pipeline=Pipeline(store);root=tmp_path/'blocked';root.write_bytes(b'not a directory')
    monkeypatch.setattr('app.pipeline.ROOT',root)
    monkeypatch.setattr('app.model_backends.model_readiness',lambda *_:{'tts_ready':True})
    try:
        with pytest.raises(OSError):pipeline.load_tts_model(Settings())
        assert not pipeline.events and not pipeline.futures
    finally:pipeline.close()
