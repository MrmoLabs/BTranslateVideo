import threading

from fastapi.testclient import TestClient
from app.server import create_app


def test_rename_trash_and_restore_keep_every_project_artifact(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store
        p=store.create('Original','en','course');identifier=p['id']
        folder=store.folder(identifier)
        (folder/'source.media').write_bytes(b'original media')
        (folder/'history').mkdir()
        (folder/'history'/'saved.txt').write_text('saved result',encoding='utf-8')
        response=client.patch(f'/api/projects/{identifier}',json={'name':'  Renamed  ','revision':0})
        assert response.status_code==200 and response.json()['name']=='Renamed'
        assert client.request('DELETE',f'/api/projects/{identifier}',json={'revision':0}).status_code==409
        assert client.request('DELETE',f'/api/projects/{identifier}',json={'revision':1}).status_code==200
        assert client.get('/api/projects').json()==[]
        trash=client.get('/api/projects/trash').json()
        assert trash[0]['id']==identifier and trash[0]['metadata_revision']==2
        assert client.get(f'/api/projects/{identifier}').status_code==404
        assert client.get(f'/api/projects/{identifier}/files/source.media').status_code==404
        assert client.post(f'/api/projects/{identifier}/run/analyze').status_code==404
        assert client.patch(f'/api/projects/{identifier}',json={'name':'bad','revision':2}).status_code==404
        assert client.post(f'/api/projects/{identifier}/restore',json={'revision':1}).status_code==409
        assert client.post(f'/api/projects/{identifier}/restore',json={'revision':2}).status_code==200
        assert client.get('/api/projects/trash').json()==[]
        assert client.get(f'/api/projects/{identifier}').json()['name']=='Renamed'
        assert (folder/'source.media').read_bytes()==b'original media'
        assert (folder/'history'/'saved.txt').read_text(encoding='utf-8')=='saved result'
        assert client.post(f'/api/projects/{identifier}/restore',json={'revision':3}).status_code==409


def test_metadata_changes_reject_busy_invalid_and_conflicting_operations(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        p=app.state.store.create('Keep me','ja','anime');identifier=p['id']
        app.state.pipeline.events[identifier]=threading.Event()
        try:
            assert client.patch(f'/api/projects/{identifier}',json={'name':'no','revision':0}).status_code==409
            assert client.request('DELETE',f'/api/projects/{identifier}',json={'revision':0}).status_code==409
        finally:app.state.pipeline.events.pop(identifier)
        assert client.patch(f'/api/projects/{identifier}',json={'name':'  ','revision':0}).status_code==400
        assert client.patch(f'/api/projects/{identifier}',json={'name':'X'*161,'revision':0}).status_code==422
        assert client.request('DELETE','/api/projects/not-an-id',json={'revision':0}).status_code==400
        assert client.request('DELETE',f'/api/projects/{identifier}',json={'revision':0},headers={'Origin':'https://example.invalid'}).status_code==403
        assert client.get(f'/api/projects/{identifier}').json()['name']=='Keep me'


def test_unavailable_projects_have_actionable_errors_and_preserve_drafts(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        p=app.state.store.create('Open in another browser','en','course');identifier=p['id']
        row=dict(id=1,start=0,end=3,text='hello',zh='你好',speaker='speaker_1')
        app.state.store.update(identifier,segments=[row],media={'duration':3})
        client.request('DELETE',f'/api/projects/{identifier}',json={'revision':0})
        payload={'segment':{**row,'zh':'unsaved audition draft'},'revision':1}
        response=client.post(f'/api/projects/{identifier}/audition',json=payload)
        assert response.status_code==404
        assert response.json()['code']=='project_deleted'
        assert response.json()['project_id']==identifier and response.json()['revision']==1
        assert not app.state.pipeline.busy(identifier)
        assert app.state.store.read(identifier,include_deleted=True)['segments']==[row]
        client.post(f'/api/projects/{identifier}/restore',json={'revision':1})
        assert client.get(f'/api/projects/{identifier}').status_code==200
        missing='f'*32
        error=client.get('/api/projects/'+missing)
        assert error.status_code==404 and error.json()['code']=='project_missing'
        assert client.get(f'/api/projects/{identifier}/files/source.media').json().get('code') is None
