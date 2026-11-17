import threading
from fastapi.testclient import TestClient
from app.server import create_app


def setup(app):
    st=app.state.store;p=st.create('roles','ja','course');id=p['id']
    row=dict(id=1,start=0,end=4,text='Hello',zh='你好',speaker='speaker_1')
    st.update(id,segments=[row],references={'speaker_1':'reference.wav'},voice_assignments={'speaker_1':{'id':'preset','name':'预设'}},outputs={'video':'result.mp4'},dub_signature='keep',diarization_proposal={'status':'ready','revision':1},subtitle_review={'status':'ready','id':'review'})
    return st,id


def test_rename_legacy_role_keeps_ids_voices_and_pending_candidates(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        response=client.put(f'/api/projects/{id}/roles/speaker_1',json={'revision':before['edit_revision'],'name':' 主角小明 ','expected_name':''})
        assert response.status_code==200,response.text
        after=response.json();assert after['roles']['speaker_1']['name']=='主角小明'
        for field in ['segments','references','voice_assignments','outputs','dub_signature','edit_revision','diarization_proposal','subtitle_review']:assert after[field]==before[field]
        assert after['metadata_revision']==before.get('metadata_revision',0)+1
        assert client.get(f'/api/projects/{id}').json()['roles']==after['roles']
        # A second tab with the original name must not silently overwrite the first.
        assert client.put(f'/api/projects/{id}/roles/speaker_1',json={'revision':after['edit_revision'],'name':'旧页面改名','expected_name':''}).status_code==409
        assert st.read(id)['roles']==after['roles']


def test_rename_rejects_stale_busy_invalid_and_unknown_roles(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/roles/speaker_1';data={'revision':before['edit_revision'],'name':'旁白','expected_name':''}
        assert client.put(url,json={**data,'revision':data['revision']+1}).status_code==409
        for name in [' ','a\nb','x'*61]:assert client.put(url,json={**data,'name':name}).status_code in {400,422}
        assert client.put(f'/api/projects/{id}/roles/missing',json=data).status_code==404
        app.state.pipeline.events[id]=threading.Event()
        try:assert client.put(url,json=data).status_code==409
        finally:app.state.pipeline.events.pop(id)
        assert st.read(id)==before


def test_manual_role_count_is_independent_of_model_speaker_limit(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);st.update(id,roles={f'speaker_{i}':{'name':f'人物{i}'} for i in range(1,62)})
        before=st.read(id)
        response=client.post(f'/api/projects/{id}/roles',json={'revision':before['edit_revision'],'name':'新增人物'})
        assert response.status_code==200
        assert response.json()['speaker']=='speaker_62' and len(response.json()['project']['roles'])==62
        assert response.json()['project']['segments']==before['segments']


def test_role_names_reject_visual_duplicates_and_keep_old_saved_name(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);p=st.update(id,roles={'speaker_1':{'name':'Alice'},'speaker_2':{'name':'Other'}})
        for name in ['alice',' Ａｌｉｃｅ ']:
            assert client.post(f'/api/projects/{id}/roles',json={'revision':p['edit_revision'],'name':name}).status_code==400
            assert client.put(f'/api/projects/{id}/roles/speaker_2',json={'revision':p['edit_revision'],'name':name,'expected_name':'Other'}).status_code==400
        assert st.read(id)['roles']==p['roles']


def test_delete_used_role_requires_destination_then_preserves_text_and_destination_voice(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);folder=st.folder(id)
        for name in ['reference.wav','destination.wav','result.mp4']:(folder/name).write_bytes(b'saved data')
        p=st.update(id,roles={'speaker_1':{'name':'Old'},'speaker_2':{'name':'Destination'}},references={'speaker_1':'reference.wav','speaker_2':'destination.wav'},reference_origins={'speaker_1':{'kind':'diarization'},'speaker_2':{'kind':'upload'}},voice_assignments={'speaker_1':{'id':'old'},'speaker_2':{'id':'keep'}})
        url=f'/api/projects/{id}/roles/speaker_1';data={'revision':p['edit_revision'],'expected_name':'Old'}
        assert client.request('DELETE',url,json=data).status_code==400
        for target in ['missing','speaker_1']:assert client.request('DELETE',url,json={**data,'replacement':target}).status_code==400
        assert st.read(id)==p
        result=client.request('DELETE',url,json={**data,'replacement':'speaker_2'})
        assert result.status_code==200,result.text;after=result.json()
        assert after['segments']==[{**p['segments'][0],'speaker':'speaker_2'}]
        for field in ['roles','references','reference_origins','voice_assignments']:assert 'speaker_1' not in after[field]
        assert after['references']['speaker_2']=='destination.wav' and after['voice_assignments']['speaker_2']=={'id':'keep'}
        assert after['reference_origins']['speaker_2']=={'kind':'upload'} and after['outputs']=={}
        assert after['edit_revision']==p['edit_revision']+1
        assert (folder/'reference.wav').exists() and list((folder/'history').glob('*/result.mp4'))
        assert client.request('DELETE',url,json=data).status_code in {404,409}


def test_delete_unused_role_keeps_audio_and_stale_or_busy_delete_is_rejected(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);p=st.update(id,roles={'speaker_2':{'name':'Unused'}})
        url=f'/api/projects/{id}/roles/speaker_2';data={'revision':p['edit_revision'],'expected_name':'Unused'}
        assert client.request('DELETE',url,json={**data,'revision':data['revision']+1}).status_code==409
        assert client.request('DELETE',url,json={**data,'expected_name':'Old tab'}).status_code==409
        app.state.pipeline.events[id]=threading.Event()
        try:assert client.request('DELETE',url,json=data).status_code==409
        finally:app.state.pipeline.events.pop(id)
        assert st.read(id)==p
        result=client.request('DELETE',url,json=data);assert result.status_code==200,result.text
        after=result.json();assert 'speaker_2' not in after['roles']
        for field in ['segments','outputs','dub_signature','references']:assert after[field]==p[field]
