import json
import threading
import time
from contextlib import contextmanager
import pytest
from fastapi.testclient import TestClient
from app.config import Settings,atomic_json
from app.server import create_app
from app.cue_refinement import selected_words,generate_candidate
from app.translation_checks import validate_values,TranslationValidationError,project_language_checks
from app.translation_quality import _request

def row(i=1,**kw):return dict(id=i,start=2,end=5,text='Hello.',zh='你好。',speaker='speaker_1',**kw)
def wait(app,id):
    end=time.monotonic()+8
    while app.state.pipeline.busy(id) and time.monotonic()<end:time.sleep(.01)
    assert not app.state.pipeline.busy(id)

@pytest.mark.parametrize('text',['我是しゅー。','欢迎クリステル小姐。','ﾊﾏﾂ先生来访。'])
def test_chinese_target_rejects_kana_including_names(text):
    with pytest.raises(TranslationValidationError,match='假名'):validate_values([row()],{1:{'zh':text}})
    assert validate_values([row()],{1:{'zh':text}},'ja')=={}

def test_chinese_target_preserves_technical_names_and_punctuation():
    assert validate_values([row()],{1:{'zh':'使用 PyTorch、C++ 与 API・测试。'}})=={}

def test_existing_kana_warning_is_derived_not_a_metadata_write(tmp_path):
    app=create_app(tmp_path/'projects');st=app.state.store;p=st.create('test','ja','course');id=p['id']
    st.update(id,segments=[{**row(),'zh':'你好，なりお。'}]);path=st.folder(id)/'project.json';before=path.read_bytes()
    with TestClient(app) as client:project=client.get(f'/api/projects/{id}').json()
    assert path.read_bytes()==before
    assert project['translation_checks']['1']['issues'][0]['code']=='target_language_mismatch'
    project['target_language']='ja';assert project_language_checks({**project,'translation_checks':{}})=={}

def test_word_midpoint_selects_only_cue_not_context_and_joins_latin():
    words=[{'word':w,'start':s,'end':e} for w,s,e in [('before',0,.5),('Hello',1,1.4),('world',1.4,1.8),('.',1.8,1.9),('after',2.1,2.5)]]
    assert selected_words([{'words':words}],1,2)=='Hello world.'
    assert selected_words([{'words':[{'word':'こ','start':1,'end':1.2},{'word':'れ','start':1.2,'end':1.4}]}],1,2)=='これ'
    assert selected_words([{'text':'untimed full context'}],1,2)==''

@pytest.fixture
def local(monkeypatch):
    @contextmanager
    def translator(*_):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.cue_refinement.local_translator',translator)
    monkeypatch.setattr('app.cue_refinement.corrected_source',lambda *a:('Hello there.','修正识别断词'))
    monkeypatch.setattr('app.cue_refinement._request',lambda *a,**k:{1:{'zh':'大家好。','reason':a[2][0]['text']}})

def setup(app):
    st=app.state.store;p=st.create('candidate','en','course');id=p['id'];(st.folder(id)/'cache').mkdir()
    st.update(id,segments=[row(),{**row(2),'start':5,'end':8}],media={'duration':8,'audio':True},outputs={'video':'result.mkv'},dub_signature='old')
    return st,id

@pytest.mark.parametrize('mode,source,translation',[('source','Hello there.',''),('translation','Hello.','大家好。'),('both','Hello there.','大家好。')])
def test_polishing_proposes_before_adoption_and_preserves_other_cues(tmp_path,monkeypatch,local,mode,source,translation):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/retranslate'
        response=client.post(url,json={'segment_id':1,'revision':before['edit_revision'],'mode':mode});assert response.status_code==200
        wait(app,id);ready=st.read(id)
        assert ready['status']=='done',ready.get('error')
        assert ready['segments']==before['segments'] and ready['outputs']==before['outputs']
        candidate=ready['retranslation'];assert candidate['after_source']==source and candidate['after']==translation
        adopted=client.post(url+'/apply',json={'revision':before['edit_revision'],'request_id':response.json()['request_id']});assert adopted.status_code==200,adopted.text
        after=adopted.json();assert after['segments'][0]['text']==source and after['segments'][0]['zh']==translation
        assert after['segments'][0]['start']==2 and after['segments'][0]['speaker']=='speaker_1'
        assert after['segments'][1]==before['segments'][1] and after['outputs']=={} and after['edit_revision']==before['edit_revision']+1
        assert (st.folder(id)/'history').exists()

def test_new_source_cannot_overwrite_later_edits(tmp_path,local):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/retranslate'
        request=client.post(url,json={'segment_id':1,'revision':before['edit_revision'],'mode':'both'}).json();wait(app,id)
        rows=before['segments'];rows[1]['zh']='用户修改另一句';manual=st.update(id,segments=rows)
        assert client.post(url+'/apply',json={'revision':manual['edit_revision'],'request_id':request['request_id']}).status_code==409
        assert st.read(id)['segments'][0]['text']=='Hello.'

def test_asr_uses_unique_16k_clip_and_keeps_timing_until_adopt(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');seen={}
    def extract(source,dest,settings,log,cancel,**kw):seen.update(kw);dest.write_bytes(b'wav')
    def transcribe(settings,audio,output,*args,**kwargs):atomic_json(output,[{'words':[{'word':'before','start':0,'end':1},{'word':'Fixed','start':2,'end':3},{'word':'after','start':5,'end':6}]}])
    monkeypatch.setattr('app.cue_refinement.extract_audio',extract);monkeypatch.setattr('app.model_backends.transcribe',transcribe)
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id);url=f'/api/projects/{id}/retranslate'
        result=client.post(url,json={'segment_id':1,'revision':before['edit_revision'],'mode':'asr'});wait(app,id)
        ready=st.read(id);assert ready['status']=='done',ready.get('error')
        assert ready['retranslation']['after_source']=='Fixed' and ready['segments']==before['segments']
        assert seen==dict(start=0,duration=7,sample_rate=16000)

def test_empty_asr_does_not_replace_current_text_or_video(tmp_path,monkeypatch):
    monkeypatch.setattr('app.cue_refinement.extract_audio',lambda *a,**kw:None)
    monkeypatch.setattr('app.model_backends.transcribe',lambda s,a,o,*args,**kwargs:atomic_json(o,[]))
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        client.post(f'/api/projects/{id}/retranslate',json={'segment_id':1,'revision':before['edit_revision'],'mode':'asr'});wait(app,id)
        after=st.read(id);assert after['status']=='failed' and '没有识别到' in after['error']
        assert after['segments']==before['segments'] and after['outputs']==before['outputs'] and not after.get('retranslation')

def test_target_language_validation_retries_then_accepts_chinese(tmp_path,monkeypatch):
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':json.dumps({'translations':[{'id':1,'zh':'你好，なりお。' if len(calls)==1 else '你好，成尾。'}]})}}]}
    async def post(url,payload,cancel):calls.append(payload);return Response()
    monkeypatch.setattr('app.translation._post_cancellable',post)
    settings=Settings(translation_model=str(tmp_path/'unused.gguf'))
    result=_request('http://127.0.0.1',settings,[row()],[],'course',threading.Event(),'draft',topic={'target_language':'zh'})
    assert result[1]['zh']=='你好，成尾。' and len(calls)==2
    assert '假名' in calls[1]['messages'][1]['content']


def test_source_polish_warns_if_model_changes_number(tmp_path,monkeypatch,local):
    monkeypatch.setattr('app.cue_refinement.corrected_source',lambda *a:('There are 9 people.','修正数字'))
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        client.post(f'/api/projects/{id}/retranslate',json={'segment_id':1,'revision':before['edit_revision'],'mode':'both'});wait(app,id)
        after=st.read(id)
        assert after['retranslation']['checks'][0]['code']=='source_number_changed'
        assert after['segments']==before['segments']

def test_cancel_source_polish_keeps_original_and_outputs(tmp_path,monkeypatch,local):
    from app.media import check_cancel
    started=threading.Event()
    def correction(*args):
        started.set();args[-1].wait(3);check_cancel(args[-1])
    monkeypatch.setattr('app.cue_refinement.corrected_source',correction)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        client.post(f'/api/projects/{id}/retranslate',json={'segment_id':1,'revision':before['edit_revision'],'mode':'source'})
        assert started.wait(2)
        client.post(f'/api/projects/{id}/cancel');wait(app,id)
        after=st.read(id);assert after['status']=='cancelled'
        assert after['segments']==before['segments'] and after['outputs']==before['outputs'] and not after.get('retranslation')

def test_native_cache_does_not_reintroduce_kana(tmp_path,monkeypatch):
    from app.translation_quality import digest
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':json.dumps({'translations':[{'id':1,'zh':'你好。'}]})}}]}
    async def post(url,payload,cancel):calls.append(payload);return Response()
    monkeypatch.setattr('app.translation._post_cancellable',post)
    settings=Settings(translation_model=str(tmp_path/'unused.gguf'));cache=tmp_path/'cache';cache.mkdir()
    args=('http://127.0.0.1',settings,[row()],[],'course',threading.Event(),'draft')
    assert _request(*args,topic={'target_language':'zh'},cache=cache)[1]['zh']=='你好。'
    path=next(cache.glob('translation-*.json'));atomic_json(path,{1:{'zh':'こんにちは。'}})
    assert _request(*args,topic={'target_language':'zh'},cache=cache)[1]['zh']=='你好。'
    assert len(calls)==2

def test_target_change_blocks_source_candidate_adoption(tmp_path,local):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,id=setup(app);before=st.read(id)
        url=f'/api/projects/{id}/retranslate'
        request=client.post(url,json={'segment_id':1,'revision':before['edit_revision'],'mode':'source'}).json();wait(app,id)
        st.update(id,target_language='ja')
        assert client.post(url+'/apply',json={'revision':before['edit_revision'],'request_id':request['request_id']}).status_code==409
        assert st.read(id)['segments']==before['segments']
