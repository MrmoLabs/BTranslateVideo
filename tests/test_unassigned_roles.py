import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.model_backends import validate_tts
from app.project_roles import project_roles,UNASSIGNED_SPEAKER
from app.server import create_app


def setup(app):
    store=app.state.store;p=store.create('last role deletion','ja','course');id=p['id'];folder=store.folder(id);(folder/'cache').mkdir()
    rows=[dict(id=i,text=f'Source {i}',zh=f'译文 {i}',speaker='speaker_1',start=(i-1)*2,end=i*2,emotion='happy') for i in range(1,4)]
    store.update(id,segments=rows,roles={'speaker_1':{'name':'主讲'}},references={'speaker_1':'ref.wav'},reference_origins={'speaker_1':{'kind':'upload'}},voice_assignments={'speaker_1':{'id':'keep-preset'}},media={'duration':6},outputs={'audio':'old.wav'})
    (folder/'ref.wav').write_bytes(b'original reference');(folder/'old.wav').write_bytes(b'original audio')
    return store,id,store.read(id)


def test_delete_last_role_keeps_every_line_and_does_not_resurrect_a_role(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,id,before=setup(app)
        response=client.request('DELETE',f'/api/projects/{id}/roles/speaker_1',json={'revision':before['edit_revision'],'expected_name':'主讲','replacement':UNASSIGNED_SPEAKER})
        assert response.status_code==200,response.text;after=response.json()
        assert project_roles(after)==set()
        for field in ['roles','references','reference_origins','voice_assignments']:assert after[field]=={}
        assert after['segments']==[{**row,'speaker':UNASSIGNED_SPEAKER} for row in before['segments']]
        assert after['outputs']=={} and after['edit_revision']==before['edit_revision']+1
        assert (store.folder(id)/'ref.wav').exists() and list((store.folder(id)/'history').glob('*/old.wav'))
        assert client.request('DELETE',f'/api/projects/{id}/roles/{UNASSIGNED_SPEAKER}',json={'revision':after['edit_revision'],'expected_name':''}).status_code==404
        assert client.get(f'/api/projects/{id}').json()['segments']==after['segments']


def test_last_role_can_be_recreated_and_all_unassigned_lines_reassigned(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,id,before=setup(app)
        after=client.request('DELETE',f'/api/projects/{id}/roles/speaker_1',json={'revision':before['edit_revision'],'expected_name':'主讲','replacement':UNASSIGNED_SPEAKER}).json()
        new=client.post(f'/api/projects/{id}/roles',json={'revision':after['edit_revision'],'name':'新的主讲'}).json()
        rows=[{**row,'speaker':new['speaker']} for row in after['segments']]
        result=client.put(f'/api/projects/{id}/segments',json={'revision':new['project']['edit_revision'],'segments':rows})
        assert result.status_code==200,result.text
        saved=store.read(id);assert project_roles(saved)=={new['speaker']}
        for old,new_row in zip(rows,saved['segments']):
            for key in ['id','text','zh','speaker','start','end','emotion']:assert new_row[key]==old[key]
        assert validate_tts(Settings(),saved,saved['segments'])=='ZH'


def test_unassigned_lines_block_dubbing_and_selected_audition_before_synthesis(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app):
        store,id,before=setup(app);row={**before['segments'][0],'speaker':UNASSIGNED_SPEAKER}
        store.update(id,segments=[row],roles={},references={},audition_request={'id':'test','segment':row})
        monkeypatch.setattr(app.state.pipeline,'synthesize',lambda *_:pytest.fail('must not load a model'))
        for action in ['dub','audition']:
            with pytest.raises(ValueError,match='尚未分配角色'):getattr(app.state.pipeline,action)(id,Settings(),threading.Event())
        assigned=before['segments'][1]
        assert validate_tts(Settings(),store.read(id),[assigned])=='ZH'


def test_unassigned_deletion_still_checks_revision_and_expected_name(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,id,before=setup(app);url=f'/api/projects/{id}/roles/speaker_1'
        data={'revision':before['edit_revision'],'expected_name':'主讲','replacement':UNASSIGNED_SPEAKER}
        assert client.request('DELETE',url,json={**data,'revision':data['revision']-1}).status_code==409
        assert client.request('DELETE',url,json={**data,'expected_name':'stale name'}).status_code==409
        assert store.read(id)==before
