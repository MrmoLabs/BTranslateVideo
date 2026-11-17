import io
import json
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.media import Cancelled,check_cancel,ffmpeg,wav_duration
from app.project_roles import reference_window
from app.server import create_app

def row(i=1,*,start=0,end=4,speaker='speaker_1',zh='原译文'):
    return dict(id=i,start=start,end=end,text='Hello.',zh=zh,speaker=speaker)

def setup(app):
    st=app.state.store;p=st.create('actions','en','course');id=p['id'];folder=st.folder(id);(folder/'cache').mkdir()
    (folder/'result.mkv').write_bytes(b'old video')
    st.update(id,segments=[row(),row(2,start=4,end=8)],media={'duration':8},outputs={'video':'result.mkv'},dub_signature='old')
    return st,id

def settled(app,id):
    deadline=time.monotonic()+8
    while app.state.pipeline.busy(id) and time.monotonic()<deadline:time.sleep(.01)
    assert not app.state.pipeline.busy(id)

def translator(monkeypatch,behavior=None):
    @contextmanager
    def local(*_):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.pipeline.local_translator',local)
    calls=[]
    def workflow(*args,**kwargs):
        calls.append((args,kwargs))
        if behavior:return behavior(*args,**kwargs)
        return {args[2][0]['id']:{'zh':'新的译文。','reason':'重新生成','checks':[]}}
    monkeypatch.setattr('app.pipeline.run_workflow',workflow)
    return calls

def test_create_unassigned_role_is_revision_checked_and_preserves_outputs(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/roles'
        assert client.post(url,json={'name':'旁白','revision':-1}).status_code==422
        app.state.pipeline.events[id]=threading.Event()
        try:assert client.post(url,json={'name':'旁白','revision':before['edit_revision']}).status_code==409
        finally:app.state.pipeline.events.pop(id)
        response=client.post(url,json={'name':'旁白','revision':before['edit_revision']});assert response.status_code==200
        created=response.json();after=created['project']
        assert created['speaker']=='speaker_2' and after['roles']['speaker_2']['name']=='旁白'
        assert after['segments']==before['segments'] and after['outputs']==before['outputs']
        assert after['edit_revision']==before['edit_revision']+1
        assert client.post(url,json={'revision':before['edit_revision']}).status_code==409
        response=client.post(url,json={'revision':after['edit_revision']})
        assert response.json()['speaker']=='speaker_3'
        assert client.get(f'/api/projects/{id}').json()['roles']['speaker_2']['name']=='旁白'

def test_unassigned_role_can_bind_library_voice_before_lines_exist(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);p=st.read(id)
        created=client.post(f'/api/projects/{id}/roles',json={'revision':p['edit_revision'],'name':'主角'}).json()
        data=io.BytesIO()
        with wave.open(data,'wb') as out:out.setparams((1,2,24000,0,'NONE','not compressed'));out.writeframes(b'\0\0'*76800)
        voice=client.post('/api/voices',data={'name':'预设'},files={'audio':('ref.wav',data.getvalue(),'audio/wav')}).json()
        response=client.put(f'/api/projects/{id}/voice/{created["speaker"]}',json={'voice_id':voice['id']})
        assert response.status_code==200 and response.json()['voice_assignments']['speaker_2']['name']=='预设'
        assert all(row['speaker']!='speaker_2' for row in response.json()['segments'])
        assert client.post(f'/api/projects/{id}/reference/speaker_2/auto',json={'revision':created['project']['edit_revision']}).status_code==400

def test_single_retranslation_proposes_then_updates_only_selected_line(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');calls=translator(monkeypatch)
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/retranslate'
        response=client.post(url,json={'segment_id':1,'revision':before['edit_revision']});assert response.status_code==200
        settled(app,id);ready=st.read(id)
        assert ready['segments']==before['segments'] and ready['outputs']==before['outputs']
        assert ready['retranslation']['before']=='原译文' and ready['retranslation']['after']=='新的译文。'
        assert calls[0][1]['cache'] is None and len(calls[0][0][2])==1
        assert calls[0][0][3][0]['id']==2
        payload={'request_id':response.json()['request_id'],'revision':ready['edit_revision']}
        assert client.post(url+'/apply',json={**payload,'revision':payload['revision']+1}).status_code==409
        after=client.post(url+'/apply',json=payload).json()
        assert after['segments'][0]['zh']=='新的译文。' and after['segments'][1]==before['segments'][1]
        assert after['outputs']=={} and (st.folder(id)/'history').exists()
        assert client.post(url+'/apply',json={**payload,'revision':after['edit_revision']}).status_code==409

def test_new_translation_cannot_overwrite_later_manual_edit(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');translator(monkeypatch)
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/retranslate'
        request=client.post(url,json={'segment_id':1,'revision':before['edit_revision']}).json();settled(app,id)
        rows=st.read(id)['segments'];rows[0]['zh']='之后手动校对的译文'
        manual=client.put(f'/api/projects/{id}/segments',json={'segments':rows,'revision':before['edit_revision']}).json()
        response=client.post(url+'/apply',json={'request_id':request['request_id'],'revision':manual['edit_revision']})
        assert response.status_code==409 and st.read(id)['segments'][0]['zh']=='之后手动校对的译文'

def test_cancelled_retranslation_keeps_original_text_and_outputs(tmp_path,monkeypatch):
    started=threading.Event()
    def work(*args,**kwargs):started.set();args[5].wait(2);check_cancel(args[5])
    app=create_app(tmp_path/'projects');translator(monkeypatch,work)
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        client.post(f'/api/projects/{id}/retranslate',json={'segment_id':1,'revision':before['edit_revision']})
        assert started.wait(2)
        client.post(f'/api/projects/{id}/cancel');settled(app,id);after=st.read(id)
        assert after['status']=='cancelled' and after['segments']==before['segments'] and after['outputs']==before['outputs']
        assert after.get('retranslation') is None

def test_reference_window_joins_short_lines_and_excludes_other_speakers():
    project={'segments':[row(start=0,end=2),row(2,start=2.1,end=8),row(3,start=3,end=4,speaker='speaker_2')]}
    assert reference_window(project,'speaker_1')=={'start':4,'end':8,'duration':4}
    with pytest.raises(ValueError,match='3 秒'):reference_window(project,'speaker_2')
    assert reference_window({'segments':[row(start=0,end=30)]},'speaker_1')['duration']==10

def test_actual_source_sampling_versions_reference_and_backs_up_old_voice(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);folder=st.folder(id)
        ffmpeg(Settings(),['-f','lavfi','-i','sine=frequency=300:duration=8','-c:a','pcm_s16le','-f','wav',str(folder/'source.media')],folder/'process.log',threading.Event())
        old=folder/'old.wav'
        with wave.open(str(old),'wb') as out:out.setparams((1,2,24000,0,'NONE','not compressed'));out.writeframes(b'\0\0'*72000)
        before=st.update(id,references={'speaker_1':'old.wav'},voice_assignments={'speaker_1':{'id':'old','name':'旧音色'}})
        response=client.post(f'/api/projects/{id}/reference/speaker_1/auto',json={'revision':before['edit_revision']});assert response.status_code==200
        settled(app,id);after=st.read(id)
        assert after['status']=='done' and after['references']['speaker_1']!='old.wav'
        assert 3<=wav_duration(folder/after['references']['speaker_1'])<=10.01
        assert after['segments']==before['segments'] and 'speaker_1' not in after['voice_assignments']
        assert after['reference_origins']['speaker_1']['kind']=='project' and after['outputs']=={}
        assert list((folder/'history').glob('*/old.wav')) and old.is_file()

def test_cancelled_source_sampling_keeps_old_bindings(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.update(id,references={'speaker_1':'old.wav'},reference_origins={'speaker_1':{'kind':'old'}})
        monkeypatch.setattr('app.pipeline.extract_audio',lambda *a,**kw:(_ for _ in ()).throw(Cancelled('cancelled')))
        client.post(f'/api/projects/{id}/reference/speaker_1/auto',json={'revision':before['edit_revision']});settled(app,id)
        after=st.read(id)
        assert after['status']=='cancelled' and after['references']==before['references'] and after['outputs']==before['outputs']
