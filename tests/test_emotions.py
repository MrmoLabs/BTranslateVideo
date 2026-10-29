import importlib.util,json,threading,time,wave
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.emotions import emotion_fields,emotion_key,emotion_vector
from app.pipeline import Pipeline,make_voice_clip
from app.server import create_app
from app.subtitles import validate_segments

def row(**changes):return dict(id=1,start=0,end=5,text='Hello.',zh='你好。',speaker='speaker_1',**changes)

def test_default_emotion_is_backward_compatible_and_manual_is_validated():
    original=row();assert validate_segments([original])==[original]
    assert validate_segments([row(emotion='inherit',emotion_strength='high')])==[original]
    expressive=validate_segments([row(emotion='sad',emotion_strength='low')])[0]
    assert expressive['emotion']=='sad' and emotion_vector(expressive)==[0,0,.35,0,0,0,0,0]
    for bad in [{'emotion':'<script>'},{'emotion_strength':'nan'},{'emotion':[]}]:
        with pytest.raises(ValueError):validate_segments([{**original,**bad}])

def test_expression_changes_cache_identity_but_default_preserves_it(tmp_path):
    settings=Settings(index_model=str(tmp_path/'model'));args=(tmp_path,'ref.wav',settings,[],'same-reference-hash')
    base=make_voice_clip(row(),*args)
    assert base==make_voice_clip(row(emotion='inherit'),*args)
    happy=make_voice_clip(row(emotion='happy'),*args)
    strong=make_voice_clip(row(emotion='happy',emotion_strength='high'),*args)
    sad=make_voice_clip(row(emotion='sad'),*args)
    assert len({base['output'],happy['output'],strong['output'],sad['output']})==4

def test_scheduler_does_not_mix_emotion_or_strength_in_a_batch(tmp_path):
    spec=importlib.util.spec_from_file_location('emotion_batch',Path(__file__).parents[1]/'scripts/tts_batch.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    clips=[dict(id=i,text='你好',reference='same.wav',output=str(tmp_path/f'{i}.wav'),**profile) for i,profile in enumerate([{},dict(emotion='happy'),{},dict(emotion='sad'),dict(emotion='happy'),dict(emotion='happy',emotion_strength='high')])]
    groups=list(module.groups(clips,4))
    assert sorted(c['id'] for g in groups for c in g)==list(range(6))
    assert all(len({emotion_key(c) for c in g})==1 for g in groups)
    assert any([c['id'] for c in g]==[1,4] for g in groups)
    with pytest.raises(module.NotBatchable):module.native_batch(None,[clips[0],clips[1]])

def setup(app,tmp_path,monkeypatch):
    store=app.state.store;p=store.create('emotion','en','course');folder=store.folder(p['id']);(folder/'cache').mkdir();(folder/'ref.wav').write_bytes(b'ref');(folder/'result.mkv').write_bytes(b'old movie')
    store.update(p['id'],segments=[row()],media={'duration':6},references={'speaker_1':'ref.wav'},outputs={'video':'result.mkv'},dub_signature='old-signature')
    model=tmp_path/'model';model.mkdir();(model/'config.yaml').write_text('test: true',encoding='utf-8')
    settings=Settings(index_model=str(model));monkeypatch.setattr('app.server.load_settings',lambda:settings)
    calls=[]
    def synthesize(self,identifier,pending,total,*args):
        calls.append(pending)
        for clip in pending:
            with wave.open(clip['output'],'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'\x00\x00'*12000)
    monkeypatch.setattr(Pipeline,'synthesize',synthesize)
    monkeypatch.setattr('app.pipeline.index_python',lambda _:Path(__file__))
    return store,p['id'],settings,calls

def settled(app,ident):
    deadline=time.monotonic()+3
    while app.state.pipeline.busy(ident) and time.monotonic()<deadline:time.sleep(.01)
    assert not app.state.pipeline.busy(ident)

def test_draft_audition_preserves_subtitles_and_movie_and_can_be_reused_after_save(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,ident,settings,calls=setup(app,tmp_path,monkeypatch);before=st.read(ident)
        draft=row(emotion='happy',emotion_strength='low');payload=dict(segment=draft,revision=before['edit_revision'])
        response=client.post(f'/api/projects/{ident}/audition',json=payload);assert response.status_code==200
        settled(app,ident);after=st.read(ident)
        assert after['segments']==before['segments'] and after['outputs']==before['outputs'] and after['dub_signature']==before['dub_signature']
        assert after['audition']['segment']==draft and calls[0][0]['emotion']=='happy'
        assert after['audition']['request_id']==response.json()['request_id']
        audio=client.get(f"/api/projects/{ident}/files/{after['audition']['file']}",headers={'Range':'bytes=0-99'})
        assert audio.status_code==206 and len(audio.content)==100
        saved=client.put(f'/api/projects/{ident}/segments',json=dict(segments=[draft],revision=before['edit_revision']))
        assert saved.status_code==200 and saved.json()['segments'][0]['emotion']=='happy'
        assert list((st.folder(ident)/'history').glob('*/result.mkv'))
        app.state.pipeline.dub(ident,settings,threading.Event())
        assert calls[-1]==[]  # Full dubbing reuses the exact preview voice.

def test_invalid_stale_and_busy_audition_requests_do_not_queue(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,ident,_,calls=setup(app,tmp_path,monkeypatch);revision=st.read(ident)['edit_revision'];url=f'/api/projects/{ident}/audition'
        assert client.post(url,json=dict(segment=row(),revision=revision-1)).status_code==409
        assert client.post(url,json=dict(segment=row(emotion='wrong'),revision=revision)).status_code==400
        assert client.post(url,json=dict(segment={**row(),'id':99},revision=revision)).status_code==400
        app.state.pipeline.events[ident]=threading.Event()
        try:assert client.post(url,json=dict(segment=row(),revision=revision)).status_code==409
        finally:app.state.pipeline.events.pop(ident)
        assert not calls

def test_failed_audition_keeps_existing_subtitles_and_outputs(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,ident,_,_=setup(app,tmp_path,monkeypatch);before=st.read(ident)
        monkeypatch.setattr(Pipeline,'synthesize',lambda *_a,**_k:(_ for _ in ()).throw(RuntimeError('model failed')))
        assert client.post(f'/api/projects/{ident}/audition',json=dict(segment=row(emotion='sad'),revision=before['edit_revision'])).status_code==200
        settled(app,ident);after=st.read(ident)
        assert after['status']=='failed' and after['segments']==before['segments'] and after['outputs']==before['outputs']
