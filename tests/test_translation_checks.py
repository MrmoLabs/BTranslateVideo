import json
import threading
from contextlib import contextmanager
import httpx
import pytest
from app.config import Settings
from app.media import Cancelled
from app.server import create_app
from app.translation import translate_batch
from app.translation_checks import numbers,uncertain_source_numbers,required_numbers,validate_values,TranslationValidationError
from app.translation_quality import _request,run_workflow

def row(text,identifier=17,zh=''):
    return dict(id=identifier,start=0,end=10,text=text,zh=zh,speaker='speaker_1')

@pytest.mark.parametrize('source,translation',[('There are 8 students.','有八个学生。'),('8 steps','八步'),('8 cats','八只猫'),('Price 1,000.00','价格1000元'),('It is 2026.','是二〇二六。'),('8.5 kg','八点五公斤'),('８ items','8个'),('8 students','٨名学生')])
def test_equivalent_written_and_unicode_numbers(source,translation):
    assert validate_values([row(source)],{17:{'zh':translation}})=={}

@pytest.mark.parametrize('source,translation',[('8 people','9人'),('23 people','二三个人'),('102 people','一百二人'),('123456789012345678901234567891 items','123456789012345678901234567892个'),('8.5 kg','85公斤'),('12 steps','120步'),('8 people','八木来了'),('8 items','这只有八卦'),('8 students','学生们'),('はい合計は8滝川さんです。','滝川先生。'),('はい8人、滝川さん。','滝川先生。')])
def test_explicit_values_and_names_are_not_silently_accepted(source,translation):
    with pytest.raises(TranslationValidationError):validate_values([row(source)],{17:{'zh':translation}})

def test_uncertain_japanese_name_fragment_is_flagged_and_source_is_unchanged():
    text='はいえーとで早速米得する8滝川に クリステルさんまた来てくれてありがとうございます。'
    source=row(text);issues=validate_values([source],{17:{'zh':'感谢滝川和克里斯特尔再次到来。'}})
    assert source['text']==text and issues[17][0]['code']=='source_numeric_uncertain'
    assert issues[17][0]['numbers']==['8'] and required_numbers(text)==[]
    assert uncertain_source_numbers('美化イチゴさん8 tmx さんこんばんは')=={'8'}
    # A second, clear occurrence in the same sentence still requires that value.
    text+=' 参加者は8人です。'
    assert required_numbers(text)==['8']
    with pytest.raises(TranslationValidationError):validate_values([row(text)],{17:{'zh':'感谢大家到来。'}})
    assert numbers('一亿二千万',written=True)=={'120000000':1}

def replies(monkeypatch,items):
    calls=[]
    async def post(url,payload,cancel):
        calls.append(payload)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'translations':items[len(calls)-1]},ensure_ascii=False)}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    return calls

def test_numeric_repair_uses_local_ids_and_explicit_constraints(monkeypatch):
    calls=replies(monkeypatch,[[{'id':1,'zh':'9人。'}],[{'id':1,'zh':'八人。'}]])
    result=_request('http://127.0.0.1',Settings(),[row('8 people')],[],'course',threading.Event(),'draft')
    assert result[17]['zh']=='八人。'
    data=json.loads(calls[0]['messages'][1]['content'].removesuffix('\n/no_think'))
    assert data['segments'][0]['required_numbers']==['8']
    assert '本批次 id=1' in calls[1]['messages'][1]['content']
    assert '第 17 段' not in calls[1]['messages'][1]['content']

def test_bad_review_keeps_validated_draft_and_emits_check(monkeypatch):
    calls=replies(monkeypatch,[[{'id':1,'zh':'8人。'}]]+[[{'id':1,'zh':'9人。'}]]*3)
    result=run_workflow('http://127.0.0.1',Settings(translation_review='always',translation_compact=False),[row('8 people')],[],'course',threading.Event())
    assert result[17]['zh']=='8人。' and len(calls)==4
    assert result[17]['checks'][0]['code']=='review_rejected'

def test_bad_compaction_keeps_full_validated_text(monkeypatch):
    replies(monkeypatch,[[{'id':1,'zh':'这次一共有8位学生来到这里参加课程。'}]]+[[{'id':1,'zh':'9人。'}]]*3)
    r=row('8 students');r['end']=.5
    result=run_workflow('http://127.0.0.1',Settings(translation_review='off'),[r],[],'course',threading.Event())
    assert '8位' in result[17]['zh'] and result[17]['checks'][0]['code']=='compact_rejected'

def test_service_error_in_review_is_not_hidden(monkeypatch):
    calls=[]
    async def post(url,payload,event):
        calls.append(payload)
        if len(calls)>1:raise httpx.ConnectError('offline')
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"translations":[{"id":1,"zh":"8人"}]}'}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post);monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    with pytest.raises(RuntimeError,match='offline'):
        run_workflow('http://127.0.0.1',Settings(translation_review='always'),[row('8 people')],[],'course',threading.Event())

def test_cancelled_review_cannot_use_draft_fallback(monkeypatch):
    event=threading.Event()
    async def post(url,payload,cancel):
        event.set()
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"translations":[{"id":1,"zh":"8人"}]}'}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    with pytest.raises(Cancelled):run_workflow('http://127.0.0.1',Settings(),[row('8 people')],[],'course',event)

def test_pipeline_persists_review_checks_with_exact_text_snapshots(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');st=app.state.store;p=st.create('checks','ja','course');id=p['id']
    source=row('はいする8滝川さんありがとう',1);st.update(id,segments=[source],media={'duration':10})
    (st.folder(id)/'cache').mkdir()
    @contextmanager
    def translator(*_):yield 'http://127.0.0.1'
    monkeypatch.setattr('app.pipeline.local_translator',translator)
    def translated(*args,**kwargs):
        kwargs['on_checks']({1:[{'code':'source_numeric_uncertain','numbers':['8']}]});return {1:'谢谢滝川先生。'}
    monkeypatch.setattr('app.pipeline.translate_batch',translated)
    try:
        app.state.pipeline.translate(id,Settings(),threading.Event())
        after=st.read(id);check=after['translation_checks']['1']
        assert after['segments'][0]['text']==source['text']
        assert check['source']==source['text'] and check['translation']=='谢谢滝川先生。' and check['target_language']=='zh'
        assert any('需人工核对' in record['message'] for record in after['logs'])
    finally:app.state.pipeline.close()


@pytest.mark.parametrize('source,translated',[
    ('北関東 の終電で20時ぐらいじゃない。','北关东的末班车大概在晚上八点左右。'),
    ('集合は20:30です。','晚上八点半集合。'),
    ('午後8時30分です。','现在是20:30。'),
    ('20時です。','现在是二十点。'),
    ('8 pm','晚上八点。'),
    ('午前12時です。','现在是0:00。'),
    ('0:00です。','晚上十二点。'),
    ('12:00です。','中午十二点。'),
])
def test_explicit_equivalent_clock_formats(source,translated):
    assert validate_values([row(source)],{17:{'zh':translated}})=={}


@pytest.mark.parametrize('source,translated',[
    ('20時です。','八点。'),
    ('20時です。','上午八点。'),
    ('20:30です。','晚上八点。'),
    ('20:30です。','晚上八点三十一分，20个人。'),
    ('20時間かかります。','晚上八点。'),
    ('20時に20人です。','晚上八点，八个人。'),
    ('20時です。','晚上八木来了。'),
    ('20:30:15です。','晚上八点半。'),
    ('20時30分です。','晚上八点三十一分。'),
    ('20 people','晚上八点。'),
    ('0:00です。','上午十二点。'),
    ('12:00です。','晚上十二点。'),
])
def test_clock_equivalence_never_waives_changed_time_or_quantity(source,translated):
    with pytest.raises(TranslationValidationError):validate_values([row(source)],{17:{'zh':translated}})
