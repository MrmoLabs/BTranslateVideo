import importlib.util
import json
import sys
import threading
import time
import types
import wave
from contextlib import contextmanager
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.server import create_app
from app.media import check_cancel
from app.config import atomic_json
from app.subtitle_review import read_review
from app.translation_quality import TranslationRequestError
from app.translation_checks import TranslationValidationError

def settled(app,id):
    deadline=time.monotonic()+8
    while app.state.pipeline.busy(id) and time.monotonic()<deadline:time.sleep(.01)
    assert not app.state.pipeline.busy(id)

def setup(app,n=4):
    store=app.state.store;p=store.create('batch','en','course');id=p['id'];folder=store.folder(id);(folder/'cache').mkdir()
    rows=[dict(id=i,text=f'Number {i}.',zh=f'旧译文{i}',start=(i-1)*3,end=i*3,speaker='speaker_1') for i in range(1,n+1)]
    store.update(id,segments=rows,media={'duration':n*3,'audio':True},outputs={'video':'result.mkv'},dub_signature='original')
    return store,id

@pytest.fixture
def translator(monkeypatch):
    calls=[];sessions=[]
    @contextmanager
    def local(*args):sessions.append('load');yield 'http://127.0.0.1'
    def request(*args,**kwargs):
        row=args[2][0];calls.append(row['id']);return {row['id']:{'zh':f'新译文{row["id"]}','reason':'优化表达'}}
    monkeypatch.setattr('app.subtitle_review.local_translator',local)
    monkeypatch.setattr('app.subtitle_review._request',request)
    monkeypatch.setattr('app.subtitle_review.corrected_source',lambda url,s,p,r,*args:(r['text']+' Corrected.','修正原文'))
    return calls,sessions,request

def start(client,store,id,ids,mode='translation'):
    response=client.post(f'/api/projects/{id}/subtitle-review',json={'revision':store.read(id)['edit_revision'],'ids':ids,'mode':mode})
    assert response.status_code==200,response.text
    return response.json()['request_id']

def test_batch_reuses_translation_session_proposes_and_adopts_subset(tmp_path,translator):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);request=start(client,st,id,[1,2,3]);settled(app,id)
        after=st.read(id);assert after['segments']==before['segments'] and after['outputs']==before['outputs']
        assert translator[0]==[1,2,3] and translator[1]==['load']
        assert after['subtitle_review']['ready']==3 and after['subtitle_review']['done']==3
        page=client.get(f'/api/projects/{id}/subtitle-review?ids=1,3').json();assert set(page['items'])=={'1','3'}
        response=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':request,'ids':[1,3],'edits':{'3':{'zh':'人工修正第三句'}}})
        assert response.status_code==200,response.text
        rows=st.read(id)['segments'];assert rows[0]['zh']=='新译文1' and rows[2]['zh']=='人工修正第三句' and rows[1]==before['segments'][1]
        # Remaining candidates survive adopting a first page of results.
        response=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':st.read(id)['edit_revision'],'request_id':request,'ids':[2]})
        assert response.status_code==200 and st.read(id)['segments'][1]['zh']=='新译文2'
        assert st.read(id)['outputs']=={} and (st.folder(id)/'history').exists()

def test_invalid_cue_skips_and_retry_preserves_completed_candidates(tmp_path,translator,monkeypatch):
    original=translator[2];failed=[True]
    def request(*args,**kw):
        if args[2][0]['id']==2 and failed[0]:raise TranslationRequestError(TranslationValidationError(2,'缺失数字'))
        return original(*args,**kw)
    monkeypatch.setattr('app.subtitle_review._request',request);app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);request_id=start(client,st,id,[1,2,3]);settled(app,id)
        review=read_review(st,id);assert [review['items'][str(i)]['status'] for i in [1,2,3]]==['ready','failed','ready']
        assert st.read(id)['segments']==before['segments']
        first=review['items']['1'].copy();failed[0]=False
        response=client.post(f'/api/projects/{id}/subtitle-review/resume',json={'revision':before['edit_revision'],'request_id':request_id,'ids':[1,2,3]});assert response.status_code==200
        settled(app,id);review=read_review(st,id);assert review['items']['1']==first and review['items']['2']['status']=='ready'
        assert translator[0]==[1,3,2]

@pytest.mark.parametrize('mode,clears',[('source',True),('both',False)])
def test_source_adoption_clears_only_obsolete_translation(tmp_path,translator,mode,clears):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);request=start(client,st,id,[1,2],mode);settled(app,id)
        result=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':request,'ids':[1,2]})
        assert result.status_code==200,result.text
        after=st.read(id)['segments'];assert after[0]['text'].endswith('Corrected.')
        assert after[0]['zh']==('' if clears else '新译文1') and after[2]==before['segments'][2]
        assert after[0]['start']==before['segments'][0]['start'] and after[0]['speaker']=='speaker_1'

def test_cancel_preserves_finished_candidates_and_resume_only_remaining(tmp_path,translator,monkeypatch):
    started=threading.Event();original=translator[2]
    def request(*args,**kwargs):
        if args[2][0]['id']==2:started.set();args[5].wait(3);check_cancel(args[5])
        return original(*args,**kwargs)
    monkeypatch.setattr('app.subtitle_review._request',request);app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);request_id=start(client,st,id,[1,2,3]);assert started.wait(3)
        client.post(f'/api/projects/{id}/cancel');settled(app,id);review=read_review(st,id)
        assert review['status']=='cancelled' and review['items']['1']['status']=='ready' and review['items']['3']['status']=='pending'
        assert st.read(id)['segments']==before['segments']
        monkeypatch.setattr('app.subtitle_review._request',original)
        result=client.post(f'/api/projects/{id}/subtitle-review/resume',json={'revision':before['edit_revision'],'request_id':request_id,'ids':[1,2,3]});assert result.status_code==200
        settled(app,id);assert translator[0]==[1,2,3] and st.read(id)['subtitle_review']['ready']==3

def test_stale_or_invalid_batch_adoption_is_atomic(tmp_path,translator):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);request=start(client,st,id,[1,2]);settled(app,id)
        rows=st.read(id)['segments'];rows[1]['zh']='后续人工修改';before=st.update(id,segments=rows)
        result=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':request,'ids':[1,2]})
        assert result.status_code==409 and st.read(id)['segments']==before['segments'] and st.read(id)['outputs']==before['outputs']
        # An unrelated changed cue does not block adoption of a still-valid one.
        result=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':request,'ids':[1],'edits':{'1':{'text':''}}})
        assert result.status_code==400 and st.read(id)['segments']==before['segments']
        result=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':request,'ids':[1]});assert result.status_code==200

def test_new_round_keeps_unselected_candidates_and_rejects_target_mismatch(tmp_path,translator):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);first=start(client,st,id,[1,2]);settled(app,id);old=read_review(st,id)['items']['1'].copy()
        second=start(client,st,id,[3]);settled(app,id);assert read_review(st,id)['items']['1']==old and first!=second
        before=st.update(id,target_language='ja')
        result=client.post(f'/api/projects/{id}/subtitle-review/apply',json={'revision':before['edit_revision'],'request_id':second,'ids':[1]});assert result.status_code==409
        assert client.post(f'/api/projects/{id}/subtitle-review',json={'revision':before['edit_revision'],'ids':[999]}).status_code==400
        assert client.get(f'/api/projects/{id}/subtitle-review?ids='+','.join(str(i) for i in range(101))).status_code==400

@pytest.mark.parametrize('provider',['faster_whisper','fun_asr'])
def test_asr_prepares_one_worker_request_for_all_clips(tmp_path,monkeypatch,provider):
    if provider=='fun_asr':
        from app.config import Settings
        monkeypatch.setattr('app.subtitle_review.load_settings',lambda:Settings(asr_provider='fun_asr'))
        monkeypatch.setattr('app.fun_asr.worker_request',lambda _:dict(model='nano',vad_model='vad',device='cpu',hotwords=[]))
    calls=[]
    def run(args,log,cancel,**kwargs):
        request=json.loads(Path(args[-1]).read_text(encoding='utf-8'));calls.append(request)
        for item in request['items']:
            atomic_json(Path(item['output']),[{'words':[{'word':'新原文','start':2.1 if item['id']>1 else .1,'end':2.5 if item['id']>1 else .5}]}]);kwargs['on_tick']()
    monkeypatch.setattr('app.subtitle_review.run',run);app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        with wave.open(str(st.folder(id)/'cache/source-16k.wav'),'wb') as output:output.setparams((1,2,16000,0,'NONE',''));output.writeframes(b'\0\0'*16000*12)
        start(client,st,id,[1,2],'asr');settled(app,id)
        assert len(calls)==1 and len(calls[0]['items'])==2
        assert st.read(id)['subtitle_review']['ready']==2 and st.read(id)['segments']==before['segments']

def test_supplied_whisper_model_is_reused_for_independent_files(tmp_path,monkeypatch):
    scripts=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(scripts))
    import model_worker
    imports=[]
    class NewModel:
        def __init__(self,*args,**kwargs):imports.append(True);raise AssertionError('must reuse the supplied model')
    monkeypatch.setitem(sys.modules,'faster_whisper',types.SimpleNamespace(WhisperModel=NewModel))
    calls=[]
    class Model:
        def transcribe(self,audio,**kwargs):calls.append(len(audio));return [],types.SimpleNamespace(duration=.1)
    model=Model()
    try:
        for i in range(2):
            audio=tmp_path/f'{i}.wav'
            with wave.open(str(audio),'wb') as out:out.setparams((1,2,16000,0,'NONE',''));out.writeframes(b'\0\0'*1600)
            model_worker.asr(dict(audio=str(audio),output=str(tmp_path/f'{i}.json'),language='en'),model=model)
        assert calls==[1600,1600] and imports==[]
    finally:sys.path.remove(str(scripts))


def test_shutdown_refuses_to_interrupt_an_active_job(tmp_path):
    app=create_app(tmp_path/'projects');called=[]
    with TestClient(app) as client:
        app.state.request_shutdown=lambda:called.append(True)
        app.state.pipeline.events['active']=threading.Event()
        try:assert client.post('/api/shutdown').status_code==409
        finally:app.state.pipeline.events.pop('active')
        assert not called
        assert client.post('/api/shutdown').status_code==200 and called==[True]

def test_asr_clip_error_keeps_other_clip_candidate(tmp_path,monkeypatch):
    def run(args,log,cancel,**kwargs):
        request=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
        for item in request['items']:
            value={'error':'本句识别失败'} if item['id']==1 else [{'words':[{'word':'New source','start':2.1,'end':2.5}]}]
            atomic_json(Path(item['output']),value);kwargs['on_tick']()
    monkeypatch.setattr('app.subtitle_review.run',run);app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        with wave.open(str(st.folder(id)/'cache/source-16k.wav'),'wb') as output:output.setparams((1,2,16000,0,'NONE',''));output.writeframes(b'\0\0'*16000*12)
        start(client,st,id,[1,2],'asr');settled(app,id)
        assert st.read(id)['subtitle_review']['ready']==1 and st.read(id)['subtitle_review']['failed']==1
        assert st.read(id)['segments']==before['segments']


@pytest.mark.parametrize('last_mode',['asr','translation'])
def test_mixed_failed_cues_resume_their_original_operations(tmp_path,translator,monkeypatch,last_mode):
    failed=[True];audio_calls=[];request_calls=[];active=[False]
    @contextmanager
    def local(*args):
        assert not active[0];active[0]=True
        try:yield 'http://127.0.0.1'
        finally:active[0]=False
    def request(*args,**kw):
        i=args[2][0]['id'];request_calls.append(i)
        if i==2 and failed[0]:raise TranslationRequestError(TranslationValidationError(2,'缺失数字'))
        return translator[2](*args,**kw)
    def asr(pipeline,identifier,project,review,settings,cancel,complete):
        assert not active[0] and review['job_ids']==[1]
        audio_calls.append(list(review['job_ids']))
        if failed[0]:complete(1,error='识别片段失败')
        else:complete(1,dict(after_source='ASR recovered source',after='',checks=[],reason='recovered'))
    monkeypatch.setattr('app.subtitle_review.local_translator',local)
    monkeypatch.setattr('app.subtitle_review._request',request)
    monkeypatch.setattr('app.subtitle_review.asr_candidates',asr)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        start(client,st,id,[3]);settled(app,id)
        completed=read_review(st,id)['items']['3'].copy()
        operations=[('asr',1),('translation',2)]
        if last_mode=='asr':operations.reverse()
        for mode,i in operations:start(client,st,id,[i],mode);settled(app,id)
        failed[0]=False;review=read_review(st,id)
        response=client.post(f'/api/projects/{id}/subtitle-review/resume',json={'revision':st.read(id)['edit_revision'],'request_id':review['id'],'ids':[1,2,3]})
        assert response.status_code==200,response.text;settled(app,id);review=read_review(st,id)
        assert review['items']['1']['after_source']=='ASR recovered source' and review['items']['1']['mode']=='asr'
        assert review['items']['2']['after']=='新译文2' and review['items']['2']['after_source']==before['segments'][1]['text']
        assert review['items']['3']==completed and audio_calls==[[1],[1]]
        assert request_calls.count(2)==2 and 1 not in request_calls
        assert st.read(id)['segments']==before['segments'] and st.read(id)['outputs']==before['outputs']
