import json
import threading
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media import Cancelled
from app.server import create_app
from app.translation import translate_batch
from app.translation_quality import TranslationRequestError, _request
from app.translation_proposals import generate_proposal, read_proposal


def test_parallel_translation_commits_in_order_with_a_stable_batch_context():
    from threading import Barrier
    from app.pipeline import translation_results

    rows = [cue(i, f'Line {i}.') for i in range(1, 5)]
    ready = Barrier(2, timeout=2)

    def request(indices, context):
        ready.wait()
        return {row['id']: row['zh'] for row in context}

    requests = translation_results(rows, list(range(4)), Settings(translation_batch=1), 2, request)
    first, first_context = next(requests)
    assert first == [0] and first_context[2] == ''
    rows[0]['zh'] = 'Prior translation'
    second, second_context = next(requests)
    assert second == [1] and second_context[1] == ''
    third, third_context = next(requests)
    assert third == [2] and third_context[1] == 'Prior translation'
    assert [indices for indices, _ in requests] == [[3]]


def cue(identifier,text,zh=''):
    return dict(id=identifier,start=identifier*3,end=identifier*3+2.8,text=text,zh=zh,speaker='speaker_1')


def test_online_translation_publishes_a_fast_batch_while_an_earlier_batch_waits():
    from app.model_backends import translation_parallelism
    from app.translation_quality import translation_results

    settings = Settings(translation_provider='openai_remote', translation_api_url='https://example.com/v1',
                        translation_remote_batch=1, translation_parallel=2)
    rows = [cue(i, f'Line {i}.') for i in range(1, 4)]
    release = threading.Event()

    def request(indices, context):
        if indices == [0]:
            assert release.wait(5)
        return {row['id']: row['zh'] for row in context}

    results = translation_results(rows, range(3), settings, translation_parallelism(settings), request)
    try:
        assert next(results)[0] == [1]
        rows[1]['zh'] = 'New translation'
        third, context = next(results)
        assert third == [2] and context[2] == ''
    finally:
        release.set()
    assert [indices for indices, _ in results] == [[0]]


@pytest.fixture
def model(monkeypatch):
    control=dict(mode='number',calls=[])
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    async def post(url,payload,cancel):
        data,_=json.JSONDecoder().raw_decode(payload['messages'][1]['content'])
        control['calls'].append(data)
        if control['mode']=='offline':raise httpx.ConnectError('service offline')
        if control['mode']=='http':return httpx.Response(500,request=httpx.Request('POST',url))
        bad=any('8' in row['text'] for row in data['segments']) and control['mode']!='fixed'
        if bad and control['mode']=='format':content='this is not JSON'
        elif bad and control['mode']=='ids':content='{"translations":[{"id":999,"zh":"wrong"}]}'
        else:
            content=json.dumps(dict(translations=[dict(id=row['id'],zh=('8个人。' if '8' in row['text'] else '有效译文。') if control['mode']=='fixed' or '8' not in row['text'] else '漏掉数字。') for row in data['segments']]),ensure_ascii=False)
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    @contextmanager
    def translator(*args):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.pipeline.local_translator',translator)
    monkeypatch.setattr('app.translation_proposals.local_translator',translator)
    return control


@pytest.mark.parametrize('mode',['number','format','ids'])
def test_isolates_bad_cue_and_keeps_siblings_without_invalid_cache(tmp_path,model,mode):
    model['mode']=mode;failures=[]
    rows=[cue(7,'Hello.'),cue(8,'8 people.'),cue(9,'Welcome.')]
    original=json.loads(json.dumps(rows))
    result=translate_batch('http://127.0.0.1',Settings(translation_review='off',translation_compact=False),rows,[],'course',threading.Event(),cache=tmp_path,on_failure=lambda row,error:failures.append((row['id'],str(error))))
    assert set(result)=={7,9} and [value[0] for value in failures]==[8]
    assert rows==original
    assert all('8' not in json.loads(path.read_text(encoding='utf-8')) for path in tmp_path.glob('translation-*.json'))
    if mode=='number':
        assert len(model['calls'])==4
        assert all([row['text'] for row in call['segments']]==['8 people.'] for call in model['calls'][1:])
        assert model['calls'][1]['context'][-1]['zh']=='有效译文。'
    else:
        assert any(len(call['segments'])==1 and call['segments'][0]['text']=='Welcome.' for call in model['calls'])


@pytest.mark.parametrize('mode',['offline','http'])
def test_global_service_errors_still_stop_and_are_never_skipped(model,mode):
    model['mode']=mode;failures=[]
    with pytest.raises(TranslationRequestError):
        translate_batch('http://127.0.0.1',Settings(),[cue(1,'Hello.')],[],'course',threading.Event(),on_failure=lambda *args:failures.append(args))
    assert not failures and len(model['calls'])==3


def test_cancel_during_skip_never_publishes_partial_success(model):
    cancel=threading.Event()
    with pytest.raises(Cancelled):
        _request('http://127.0.0.1',Settings(),[cue(1,'8 people.')],[],'course',cancel,'draft',on_failure=lambda *_:cancel.set())


def test_strict_single_cue_call_still_reports_failure(model):
    with pytest.raises(TranslationRequestError):
        _request('http://127.0.0.1',Settings(),[cue(339,'8 people.')],[],'course',threading.Event(),'draft')


def populate(app):
    st=app.state.store;p=st.create('跳过错误验收','en','course');identifier=p['id']
    st.update(identifier,media=dict(duration=100),segments=[cue(1,'Old.','人工旧译文。'),cue(2,'8 people.'),cue(3,'Hello.'),cue(4,'Welcome.')])
    (st.folder(identifier)/'cache').mkdir()
    return st,identifier


def test_pipeline_continues_later_batches_surfaces_errors_and_resumes_only_missing(tmp_path,model):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,identifier=populate(app);pipe=app.state.pipeline
        settings=Settings(translation_batch=2,translation_review='off',translation_compact=False)
        pipe.submit(identifier,'translate',settings)
        pipe.futures[identifier].result(timeout=10)
        after=st.read(identifier);rows=after['segments']
        assert after['status']=='done' and after['error']==''
        assert rows[0]['zh']=='人工旧译文。' and not rows[1]['zh']
        assert rows[2]['zh'] and rows[3]['zh']
        assert after['translation_checks']['2']['source']=='8 people.'
        assert after['translation_checks']['2']['translation']==''
        assert after['translation_checks']['2']['issues'][0]['code']=='translation_failed'
        assert '待人工处理' in after['processing']['message']
        assert any('跳过待处理 1 段' in record['message'] for record in after['logs'])
        assert (st.folder(identifier)/'chinese.srt').exists()
        model['mode']='fixed';model['calls'].clear()
        pipe.submit(identifier,'translate',settings);pipe.futures[identifier].result(timeout=10)
        after=st.read(identifier)
        assert after['segments'][1]['zh']=='8个人。' and '2' not in after['translation_checks']
        assert all(len(call['segments'])==1 and call['segments'][0]['text']=='8 people.' for call in model['calls'])


def test_all_finishes_other_translation_and_waits_before_incomplete_dubbing(tmp_path,model,monkeypatch):
    app=create_app(tmp_path/'projects')
    with TestClient(app):
        st,identifier=populate(app);pipe=app.state.pipeline
        monkeypatch.setattr('app.pipeline.validate_tts',lambda *_:None)
        monkeypatch.setattr(pipe,'analyze',lambda *_:None)
        def forbidden(*_):pytest.fail('Dubbing/export must wait for missing translations')
        monkeypatch.setattr(pipe,'dub',forbidden);monkeypatch.setattr(pipe,'export',forbidden)
        pipe.submit(identifier,'all',Settings(translation_review='off',translation_compact=False))
        pipe.futures[identifier].result(timeout=10)
        after=st.read(identifier)
        assert after['status']=='done' and after['stage']=='translate' and not after['error']
        assert after['processing']['completed_steps']==['analyze','translate']
        assert after['segments'][-1]['zh'] and not after['segments'][1]['zh']


def test_batch_review_keeps_bad_cue_old_text_and_exposes_it_without_caching_failure(tmp_path,model):
    app=create_app(tmp_path/'projects')
    with TestClient(app):
        st,identifier=populate(app);rows=st.read(identifier)['segments']
        rows[1]['zh']='旧译文不动。';rows[2]['zh']='原译文。';rows[3]['zh']='另一句。'
        st.update(identifier,segments=rows,outputs={'video':'prior.mkv'})
        settings=Settings(translation_review='off',translation_compact=False)
        generate_proposal(st,identifier,settings,threading.Event(),app.state.pipeline.progress)
        after=st.read(identifier);proposal=read_proposal(st,identifier)
        assert after['segments']==rows and after['outputs']=={'video':'prior.mkv'}
        assert proposal['failed']==1 and len(proposal['items'])==4
        bad=next(item for item in proposal['items'] if item['id']==2)
        assert bad['before']==bad['after']=='旧译文不动。' and bad['checks'][0]['code']=='translation_failed'
        assert after['translation_checks']['2']['translation']=='旧译文不动。'
        journal=next((st.folder(identifier)/'cache').glob('proposal-*.json'))
        assert '2' not in json.loads(journal.read_text(encoding='utf-8'))
        model['mode']='fixed';model['calls'].clear()
        generate_proposal(st,identifier,settings,threading.Event(),app.state.pipeline.progress)
        assert read_proposal(st,identifier)['failed']==0
        assert all(len(call['segments'])==1 and call['segments'][0]['text']=='8 people.' for call in model['calls'])


@pytest.mark.parametrize('phase',['review','compact'])
def test_invalid_optional_model_output_keeps_validated_draft(monkeypatch,phase):
    from app.translation_quality import run_workflow
    calls=[]
    async def post(url,payload,cancel):
        calls.append(payload)
        content='{"translations":[{"id":1,"zh":"一共有8个人来这里参加活动。"}]}' if len(calls)==1 else 'invalid JSON'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    settings=Settings(translation_review='always' if phase=='review' else 'off',translation_compact=phase=='compact')
    row=cue(1,'8 people.');row['end']=row['start']+.5
    result=run_workflow('http://127.0.0.1',settings,[row],[],'course',threading.Event())
    assert '8' in result[1]['zh'] and result[1]['checks'][0]['code']==phase+'_rejected'
    assert len(calls)==4
