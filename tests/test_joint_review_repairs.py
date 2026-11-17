import json
import threading
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media import Cancelled
from app.server import create_app
from app.subtitle_review import read_review
from app.translation_checks import TranslationValidationError, japanese_fragments, validate_values
from app.translation_quality import TranslationRequestError, _request


def cue(text,identifier=17):
    return dict(id=identifier,text=text,zh='',start=0,end=10,speaker='speaker_1')


@pytest.mark.parametrize('source,target',[
    ('2年3年はかかる','需要两三年'),('2回、3回ぐらい','两三次左右'),
    ('3コマ伸びた','伸长三格'),('3 frames','三帧'),('2丁目','两丁目'),
    ('2年から3年','两三年'),('8-9個','八九个'),
])
def test_source_grounded_colloquial_numbers(source,target):
    assert validate_values([cue(source)],{17:{'zh':target}})=={}


@pytest.mark.parametrize('source,target',[
    ('23年','两三年'),('2年3年','两三次'),('2年4年','两三年'),
    ('2年3年、3人','两三年，还有人'),('8 items','八九个'),('3 pages','三木'),
])
def test_range_repair_cannot_hide_other_missing_or_changed_numbers(source,target):
    with pytest.raises(TranslationValidationError):validate_values([cue(source)],{17:{'zh':target}})


def mock_responses(monkeypatch,values,cancel=None):
    calls=[]
    async def post(url,payload,event):
        calls.append(payload)
        value=values[len(calls)-1]
        if isinstance(value,Exception):raise value
        if cancel and len(calls)==3:cancel.set()
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    return calls


def test_foreign_fragment_repair_preserves_surrounding_facts_and_neutral_honorific(monkeypatch):
    rejected={'translations':[{'id':1,'zh':'なりおさん买了8根ポッキー吗？'}]}
    calls=mock_responses(monkeypatch,[rejected,rejected,{'replacements':[
        {'source':'なりお','target':'成尾'},{'source':'ポッキー','target':'百奇'}]}])
    result=_request('http://127.0.0.1',Settings(),[cue('なりおさんはポッキーを8本買った？')],[],'anime',threading.Event(),'draft')
    assert result[17]['zh']=='成尾买了8根百奇吗？'
    assert result[17]['checks'][0]['code']=='foreign_fragment_repaired'
    assert len(calls)==3
    assert japanese_fragments('ﾎﾟｯｷｰ、ポッキー。')==['ポッキー','ポッキー']
    repair=json.loads(calls[1]['messages'][1]['content'].removesuffix('\n/no_think'))
    assert repair['rejected_translation'][0]['zh']==rejected['translations'][0]['zh']
    assert '本批次 id=1' in repair['validation_error']


@pytest.mark.parametrize('target',['なりお','成尾有9个人',''])
def test_foreign_fragment_repair_rejects_unfixed_or_invented_information(monkeypatch,target):
    rejected={'translations':[{'id':1,'zh':'なりお来了。'}]}
    mock_responses(monkeypatch,[rejected,rejected,{'replacements':[{'source':'なりお','target':target}]}])
    with pytest.raises(TranslationRequestError):
        _request('http://127.0.0.1',Settings(),[cue('なりおさんが来ました')],[],'anime',threading.Event(),'draft')


def test_fragment_repair_cancellation_is_not_returned_as_success(monkeypatch):
    rejected={'translations':[{'id':1,'zh':'なりお来了。'}]};cancel=threading.Event()
    mock_responses(monkeypatch,[rejected,rejected,{'replacements':[{'source':'なりお','target':'成尾'}]}],cancel)
    with pytest.raises(Cancelled):
        _request('http://127.0.0.1',Settings(),[cue('なりおさんが来ました')],[],'anime',cancel,'draft')


def test_fragment_repair_service_failure_remains_system_error(monkeypatch):
    rejected={'translations':[{'id':1,'zh':'なりお来了。'}]}
    mock_responses(monkeypatch,[rejected,rejected,httpx.ConnectError('offline')])
    with pytest.raises(TranslationRequestError) as error:
        _request('http://127.0.0.1',Settings(),[cue('なりおさんが来ました')],[],'anime',threading.Event(),'draft')
    assert not error.value.recoverable


def setup_batch(app):
    store=app.state.store;project=store.create('auto-repair','ja','course');identifier=project['id']
    (store.folder(identifier)/'cache').mkdir();(store.folder(identifier)/'source.media').write_bytes(b'test media')
    rows=[{**cue(f'Original {i}',i),'start':(i-1)*10,'end':i*10} for i in [1,2,3]]
    store.update(identifier,segments=rows,media={'duration':30,'audio':True},outputs={'video':'old.mp4'})
    return store,identifier,store.read(identifier)


def wait_for_batch(app,identifier):
    import time
    end=time.monotonic()+8
    while app.state.pipeline.busy(identifier) and time.monotonic()<end:time.sleep(.01)
    assert not app.state.pipeline.busy(identifier)


def test_joint_review_recovers_uncertain_and_invalid_cues_in_shared_audio_phase(tmp_path,monkeypatch):
    active=[False];events=[]
    @contextmanager
    def translator(*_):
        assert not active[0];active[0]=True;events.append('text_open')
        try:yield 'http://127.0.0.1'
        finally:active[0]=False;events.append('text_close')
    def source(url,settings,project,row,*_):return row['text'],'无法确定，需要听音确认' if row['id']==1 else ''
    def request(url,settings,rows,*args,**kw):
        row=rows[0];events.append(row['text'])
        if row['id']==2 and row['text'].startswith('Original'):raise TranslationRequestError(TranslationValidationError(2,'缺失或改写数字：8'))
        return {row['id']:{'zh':'已翻译：'+row['text']}}
    def asr(pipeline,identifier,project,review,settings,cancel,complete):
        assert not active[0];events.append('audio');assert review['job_ids']==[1,2]
        for i in review['job_ids']:complete(i,{'after_source':f'Recovered {i}'})
    monkeypatch.setattr('app.subtitle_review.local_translator',translator)
    monkeypatch.setattr('app.subtitle_review.corrected_source',source)
    monkeypatch.setattr('app.subtitle_review._request',request)
    monkeypatch.setattr('app.subtitle_review.asr_candidates',asr)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,identifier,before=setup_batch(app)
        response=client.post(f'/api/projects/{identifier}/subtitle-review',json={'revision':before['edit_revision'],'ids':[1,2,3],'mode':'both'})
        assert response.status_code==200;wait_for_batch(app,identifier)
        review=read_review(store,identifier)
        assert all(review['items'][str(i)]['status']=='ready' for i in [1,2,3])
        assert review['items']['2']['after_source']=='Recovered 2'
        assert review['items']['2']['checks'][0]['code']=='source_audio_reanalyzed'
        assert events.index('Original 3')<events.index('audio')
        assert events.count('text_open')==2 and events.count('audio')==1
        after=store.read(identifier)
        for field in ['segments','edit_revision','outputs']:assert after[field]==before[field]


def test_audio_failure_is_per_cue_and_resume_reuses_durable_transcript(tmp_path,monkeypatch):
    @contextmanager
    def translator(*_):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.subtitle_review.local_translator',translator)
    monkeypatch.setattr('app.subtitle_review.corrected_source',lambda *args:(args[3]['text'],'不确定，请听音'))
    fail=[True];audio_calls=[]
    def request(*args,**kw):
        row=args[2][0]
        if row['id']==2 and fail[0]:raise TranslationRequestError(ValueError('模型 JSON 错误'))
        return {row['id']:{'zh':'译文'+row['text']}}
    def asr(pipeline,identifier,project,review,settings,cancel,complete):
        audio_calls.append(review['job_ids'])
        complete(1,error='无有效语音');complete(2,{'after_source':'Recovered 2'})
    monkeypatch.setattr('app.subtitle_review._request',request)
    monkeypatch.setattr('app.subtitle_review.asr_candidates',asr)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,identifier,before=setup_batch(app)
        result=client.post(f'/api/projects/{identifier}/subtitle-review',json={'revision':before['edit_revision'],'ids':[1,2],'mode':'both'}).json()
        wait_for_batch(app,identifier);review=read_review(store,identifier)
        assert review['items']['1']['status']=='failed' and review['items']['2']['status']=='failed'
        assert review['items']['2']['audio_repair']['source']=='Recovered 2'
        fail[0]=False
        response=client.post(f'/api/projects/{identifier}/subtitle-review/resume',json={'revision':before['edit_revision'],'request_id':result['request_id'],'ids':[2]})
        assert response.status_code==200;wait_for_batch(app,identifier)
        assert read_review(store,identifier)['items']['2']['status']=='ready'
        assert audio_calls==[[1,2]]
        assert store.read(identifier)['segments']==before['segments']


def test_cancelled_audio_repair_resumes_with_saved_transcript(tmp_path,monkeypatch):
    @contextmanager
    def translator(*_):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.subtitle_review.local_translator',translator)
    monkeypatch.setattr('app.subtitle_review.corrected_source',lambda *args:(args[3]['text'],'不确定，请听音'))
    monkeypatch.setattr('app.subtitle_review._request',lambda *args,**kw:{args[2][0]['id']:{'zh':'已译'+args[2][0]['text']}})
    audio_calls=[]
    def asr(pipeline,identifier,project,review,settings,cancel,complete):
        audio_calls.append(list(review['job_ids']))
        for i in review['job_ids']:
            complete(i,{'after_source':f'Recovered {i}'})
            if len(audio_calls)==1:raise Cancelled()
    monkeypatch.setattr('app.subtitle_review.asr_candidates',asr)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store,identifier,before=setup_batch(app)
        result=client.post(f'/api/projects/{identifier}/subtitle-review',json={'revision':before['edit_revision'],'ids':[1,2],'mode':'both'}).json()
        wait_for_batch(app,identifier);review=read_review(store,identifier)
        assert review['status']=='cancelled' and review['items']['1']['audio_repair']['source']=='Recovered 1'
        response=client.post(f'/api/projects/{identifier}/subtitle-review/resume',json={'revision':before['edit_revision'],'request_id':result['request_id'],'ids':[1,2]})
        assert response.status_code==200;wait_for_batch(app,identifier)
        assert read_review(store,identifier)['items']['1']['status']=='ready'
        assert read_review(store,identifier)['items']['2']['status']=='ready'
        assert audio_calls==[[1,2],[2]]
        assert store.read(identifier)['segments']==before['segments']
