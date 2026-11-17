import json
import pytest
from app.store import Store
from app.file_io import atomic_json


def test_runtime_updates_never_rewrite_long_content(tmp_path):
    store=Store(tmp_path);p=store.create('checkpoint','en','course');identifier=p['id']
    rows=[dict(id=i,text='word '*100,zh='',words=[dict(word='word',start=n,end=n+1) for n in range(20)]) for i in range(1000)]
    store.update(identifier,segments=rows)
    path=store.folder(identifier)/'project.json';before=path.read_bytes();stamp=path.stat().st_mtime_ns
    for tick in range(12):store.update_runtime(identifier,status='running',progress=tick,logs=[dict(message=str(tick))])
    assert path.read_bytes()==before and path.stat().st_mtime_ns==stamp
    side=path.with_name('project-state.json')
    assert len(side.read_bytes())<len(before)/100
    assert store.read(identifier)['progress']==11
    assert store.read(identifier)['segments']==rows
    assert Store(tmp_path).read(identifier)['progress']==11
    assert not side.read_bytes().startswith(b'\xef\xbb\xbf')


def test_content_checkpoint_merges_state_and_invalidates_old_snapshot(tmp_path):
    store=Store(tmp_path);identifier=store.create('legacy','en','course')['id']
    path=store.folder(identifier)/'project.json';before=path.read_bytes()
    store.read(identifier);assert path.read_bytes()==before
    store.update_runtime(identifier,status='running',progress=58)
    store.update(identifier,name='renamed')
    base=json.loads(path.read_text(encoding='utf-8'))
    assert base['progress']==58 and base['name']=='renamed'
    assert store.read(identifier)['progress']==58
    # Restoring/replacing a checkpoint must not overlay a stale progress snapshot.
    atomic_json(path,{**base,'progress':9,'status':'idle'})
    assert store.read(identifier)['progress']==9
    assert store.read(identifier)['status']=='idle'


def test_snapshot_allowlist_failure_and_detached_reads(tmp_path,monkeypatch):
    import app.project_state as module
    store=Store(tmp_path);identifier=store.create('safe','en','course')['id']
    store.update_runtime(identifier,progress=2)
    path=store.folder(identifier)/'project-state.json'
    side=json.loads(path.read_text());side['state']['segments']=[{'bad':True}];atomic_json(path,side)
    assert store.read(identifier)['segments']==[]
    def fail(*args):raise PermissionError('injected')
    with monkeypatch.context() as patch:
        patch.setattr(module,'atomic_json',fail)
        with pytest.raises(PermissionError):store.update_runtime(identifier,progress=99)
    assert store.read(identifier)['progress']==2
    result=store.read(identifier);result['segments'].append({'bad':True})
    assert store.read(identifier)['segments']==[]
    with pytest.raises(ValueError):store.update_runtime(identifier,segments=[])


@pytest.mark.parametrize('raw',['{bad','[]','null','{"version":1,"state":[]}'])
def test_bad_sidecar_keeps_last_checkpoint(tmp_path,raw):
    store=Store(tmp_path);identifier=store.create('fallback','en','course')['id']
    (store.folder(identifier)/'project-state.json').write_text(raw,encoding='utf-8')
    assert store.read(identifier)['status']=='idle'


def test_recovery_and_deleted_runtime(tmp_path):
    from app.store import ProjectUnavailable
    store=Store(tmp_path);identifier=store.create('recover','en','course')['id']
    store.update_runtime(identifier,status='running');Store(tmp_path).recover()
    assert store.read(identifier)['status']=='interrupted'
    store.update(identifier,deleted_at='today')
    with pytest.raises(ProjectUnavailable):store.read_runtime(identifier)
    with pytest.raises(ProjectUnavailable):store.update_runtime(identifier,progress=1)


def test_http_polling_transfers_runtime_without_subtitle_arrays(tmp_path):
    from fastapi.testclient import TestClient
    from app.server import create_app
    app=create_app(tmp_path/'projects');store=app.state.store;identifier=store.create('http','en','course')['id']
    store.update(identifier,segments=[dict(id=1,text='large source',zh='')])
    with TestClient(app) as client:
        base=client.get(f'/api/projects/{identifier}').json()
        store.update_runtime(identifier,progress=45,status='running')
        packet=client.get(f'/api/projects/{identifier}/runtime').json()
        assert packet['content_version']==base['content_version']
        assert 'segments' not in packet and 'references' not in packet
        assert packet['progress']==45
        (store.folder(identifier)/'process.log').write_text('latest diagnostics',encoding='utf-8')
        assert client.get(f'/api/projects/{identifier}/runtime').json()['process_log']=='latest diagnostics'
        store.update(identifier,segments=[dict(id=1,text='edited source',zh='')])
        assert client.get(f'/api/projects/{identifier}/runtime').json()['content_version']!=base['content_version']
        store.update_runtime(identifier,status='idle')
