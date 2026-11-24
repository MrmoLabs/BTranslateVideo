import json
import threading
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from app.config import Settings, atomic_json
from app.media import Cancelled
from app.server import create_app
from app.translation import translate_batch
from app.translation_quality import digest, translation_batches, translation_context, speech_seconds, run_workflow
from app.translation_proposals import generate_proposal

def row(i=1, text="Only after 12 steps does the model converge.", zh="", **extra):
    return dict(id=i,start=(i-1)*10,end=i*10,text=text,zh=zh,speaker="speaker_1",**extra)

def stub(monkeypatch, replies):
    calls=[]
    async def post(url,payload,cancel):
        calls.append(payload)
        items=replies[len(calls)-1]
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"translations":items},ensure_ascii=False)}}]},request=httpx.Request("POST",url))
    monkeypatch.setattr("app.translation._post_cancellable",post)
    return calls

def test_context_is_chronological_and_sparse_holes_are_not_joined():
    rows=[row(i+1) for i in range(9)]
    assert list(translation_batches(rows,[0,1,4,5,8],6))==[[0,1],[4,5],[8]]
    context=translation_context(rows,[3,4])
    assert [r["id"] for r in context]==[1,2,3,6,7,8]
    assert [r["position"] for r in context]==["before"]*3+["after"]*3
    rows[0]["text"]="x"*2000;rows[1]["text"]="x"*2000
    assert list(translation_batches(rows,[0,1],6))==[[0],[1]]

def test_auto_review_corrects_draft_and_preserves_values(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="只有在12步之后模型做收敛。")],[dict(id=1,zh="完成12步后，模型才会收敛。",reason="还原强调语序")]])
    result=translate_batch("http://127.0.0.1",Settings(translation_compact=False),[row()],[],"course",threading.Event())
    assert result=={1:"完成12步后，模型才会收敛。"} and len(calls)==2
    data=json.loads(calls[1]["messages"][1]["content"].removesuffix("\n/no_think"))
    assert data["segments"][0]["draft"]=="只有在12步之后模型做收敛。"

def test_existing_translation_uses_independent_draft_and_filters_trivial_punctuation(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="告诉模型它的维度……")],[dict(id=1,zh="告诉模型它的维度……",reason="仅改标点")]])
    old=row(text="Tell the model its dimension.",zh="告诉模型它的维度…")
    result=run_workflow("http://127.0.0.1",Settings(translation_compact=False),[old],[],"course",threading.Event(),existing=True)
    first=json.loads(calls[0]["messages"][1]["content"].removesuffix("\n/no_think"))
    second=json.loads(calls[1]["messages"][1]["content"].removesuffix("\n/no_think"))
    assert "previous_translation" not in first["segments"][0]
    assert "previous_translation" not in second["segments"][0]
    assert result[1]["zh"]==old["zh"] and result[1]["reason"]==""

def test_simple_sentence_avoids_extra_review_and_compaction_is_conditional(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="你好。")]])
    assert translate_batch("http://127.0.0.1",Settings(),[row(text="Hello.")],[],"course",threading.Event())=={1:"你好。"}
    assert len(calls)==1
    calls=stub(monkeypatch,[[dict(id=1,zh="现在让我们来创建我们的第一个文件。")],[dict(id=1,zh="创建第一个文件。")]])
    result=translate_batch("http://127.0.0.1",Settings(translation_review="off"),[dict(row(text="Create the first file."),end=1.5)],[],"course",threading.Event())
    assert result=={1:"创建第一个文件。"} and len(calls)==2


def test_custom_system_prompt_reaches_draft_review_and_compaction(monkeypatch):
    long_translation='各位同学，欢迎大家来到今天的沟通技巧课堂。'
    replies=[[dict(id=1,zh=long_translation)],[dict(id=1,zh=long_translation)],[dict(id=1,zh='各位好。')]]
    calls=stub(monkeypatch,replies)
    settings=Settings(translation_system_prompt='  按讲者和学生对话的口吻翻译。\n避免书面套话。  ',
                      translation_review='always')
    result=run_workflow('http://localhost',settings,[dict(row(text='Welcome, everyone.'),end=1)],[],
                        'course',threading.Event())
    assert result[1]['zh']=='各位好。' and len(calls)==3
    for call in calls:
        prompt=call['messages'][0]
        assert prompt['role']=='system' and prompt['content'].startswith('按讲者和学生对话的口吻翻译。\n避免书面套话。')
        assert 'target_ids' in prompt['content'] and '输出语言为简体中文' in prompt['content']


def test_custom_system_prompt_uses_official_bilibili_format(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh='各位好。')]])
    settings=Settings(translation_provider='bilibili',translation_system_prompt='按口语表达。',
                      translation_review='off',translation_compact=False)
    run_workflow(settings.translation_service_url,settings,[row(text='Welcome, everyone.')],[],'course',threading.Event())
    message,=calls[0]['messages']
    assert message['role']=='user' and '按口语表达。' in message['content']
    assert '〖源文〗' in message['content'] and '只输出 JSON' in message['content']


def test_online_translation_splits_a_truncated_batch_without_repeating_it(monkeypatch):
    sizes=[]

    async def post(url,payload,cancel,**options):
        data=json.loads(payload['messages'][1]['content'].removesuffix('\n/no_think'))
        ids=data['target_ids']
        sizes.append(len(ids))
        if len(ids)>2:
            choice=dict(finish_reason='length',message=dict(content=''))
        else:
            text=json.dumps({'translations':[dict(id=identifier,zh='各位好。') for identifier in ids]})
            choice=dict(finish_reason='stop',message=dict(content=text))
        return httpx.Response(200,json={'choices':[choice]},request=httpx.Request('POST',url))

    monkeypatch.setattr('app.translation._post_cancellable',post)
    settings=Settings(translation_provider='openai_remote',translation_api_url='https://example.com/v1',
                      translation_review='off',translation_compact=False)
    rows=[row(i,text='Hello.') for i in range(1,5)]
    result=translate_batch(settings.translation_service_url,settings,rows,[],'course',threading.Event())
    assert set(result)=={1,2,3,4} and sizes==[4,2,2]

def test_literal_number_change_is_retried_with_feedback(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="需要120步。")],[dict(id=1,zh="需要12步。")]])
    monkeypatch.setattr(threading.Event,"wait",lambda *_: None)
    result=translate_batch("http://127.0.0.1",Settings(translation_review="off",translation_compact=False),[row()],[],"course",threading.Event())
    assert result[1]=="需要12步。" and "未通过校验" in calls[1]["messages"][1]["content"]

def test_question_omission_in_review_and_compaction_is_retried(monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="输入是什么？输入是12维向量。")],[dict(id=1,zh="输入是12维向量。")],[dict(id=1,zh="输入是什么？输入是12维向量。")],[dict(id=1,zh="输入是12维向量。")],[dict(id=1,zh="什么输入？12维向量。")]])
    monkeypatch.setattr(threading.Event,"wait",lambda *_: None)
    result=run_workflow("http://127.0.0.1",Settings(translation_review="always"),[dict(row(text="What is the input? It is a 12-dimensional vector."),end=1)],[],"course",threading.Event())
    assert result[1]["zh"]=="什么输入？12维向量。" and len(calls)==5
    assert all("遗漏原文问句" in calls[i]["messages"][1]["content"] for i in [2,4])


def test_local_review_retries_only_the_line_that_failed_validation(monkeypatch):
    calls=stub(monkeypatch,[
        [dict(id=1,zh='您好。'),dict(id=2,zh='有8个人。')],
        [dict(id=1,zh='你好。'),dict(id=2,zh='有人。')],
        [dict(id=1,zh='一共有8人。')],
    ])
    rows=[row(1,text='Hello.'),row(2,text='There are 8 people.')]
    result=translate_batch("http://127.0.0.1",Settings(translation_review="always",translation_compact=False),
                           rows,[],"course",threading.Event())
    assert result=={1:'你好。',2:'一共有8人。'}
    retry=json.loads(calls[2]["messages"][1]["content"].removesuffix("\n/no_think"))
    assert [segment['text'] for segment in retry['segments']]==['There are 8 people.']
    assert retry['context'][0]['zh']=='你好。'

def test_unchanged_save_preserves_outputs_and_proposal(tmp_path):
    app=create_app(tmp_path/"projects")
    with TestClient(app) as client:
        st,ident=setup_project(app);proposal(app,ident);before=st.read(ident)
        response=client.put(f"/api/projects/{ident}/segments",json=dict(segments=before["segments"],revision=before["edit_revision"]))
        assert response.status_code==200 and response.json()==before
        assert not client.get(f"/api/projects/{ident}/translation-proposal").json()["stale"]

def test_cancellation_before_review_cannot_publish_draft(monkeypatch):
    event=threading.Event()
    async def post(*args):
        event.set()
        return httpx.Response(200,json={"choices":[{"message":{"content":'{"translations":[{"id":1,"zh":"12步后收敛。"}]}'}}]},request=httpx.Request("POST","http://127.0.0.1"))
    monkeypatch.setattr("app.translation._post_cancellable",post)
    with pytest.raises(Cancelled):
        translate_batch("http://127.0.0.1",Settings(),[row()],[],"course",event)

def test_cached_draft_and_review_reused_but_new_context_invalidates(tmp_path,monkeypatch):
    calls=stub(monkeypatch,[[dict(id=1,zh="12步后才收敛。")],[dict(id=1,zh="完成12步后才会收敛。")],[dict(id=1,zh="12步后才收敛。")],[dict(id=1,zh="完成12步后才会收敛。")]])
    settings=Settings(translation_compact=False)
    for _ in range(2): translate_batch("http://127.0.0.1",settings,[row()],[],"course",threading.Event(),cache=tmp_path)
    assert len(calls)==2
    translate_batch("http://127.0.0.1",settings,[row()],[dict(row(2),position="after")],"course",threading.Event(),cache=tmp_path)
    assert len(calls)==4

def setup_project(app):
    st=app.state.store;p=st.create("quality","en","course");ident=p["id"];folder=st.folder(ident);(folder/"cache").mkdir()
    st.update(ident,media={"duration":30},segments=[row(zh="只有在12步之后它做收敛。"),row(2,text="Hello.",zh="你好。")],outputs={"audio":"chinese.wav","video":"result.mkv"},dub_signature="keep")
    (folder/"result.mkv").write_bytes(b"old video");(folder/"chinese.wav").write_bytes(b"old audio")
    return st,ident


def test_retranslate_all_queues_fresh_candidates_including_untranslated_cues(tmp_path, monkeypatch):
    settings = Settings(translation_layout='paragraph', translation_review='off')
    monkeypatch.setattr('app.server.load_settings', lambda: settings)
    from contextlib import contextmanager
    @contextmanager
    def local(*_):
        yield 'http://127.0.0.1'

    calls = []
    second_batch = threading.Event()
    release_batch = threading.Event()
    def workflow(url, config, batch, context, mode, cancel, **options):
        assert config.translation_layout == 'paragraph'
        assert options['existing'] is False and options['cache'] is None
        assert all(not item['zh'] for item in [*batch, *context])
        calls.extend(item['id'] for item in batch)
        if batch[0]['id'] == 2:
            second_batch.set()
            assert release_batch.wait(5)
        return {item['id']: dict(zh=f'新译文{item["id"]}', reason='', estimated_seconds=2) for item in batch}

    monkeypatch.setattr('app.translation_proposals.local_translator', local)
    monkeypatch.setattr('app.translation_proposals.run_workflow', workflow)
    app = create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store, identifier = setup_project(app)
        initial = store.read(identifier)
        rows = initial['segments'] + [row(3, text='One more.')]
        rows[1]['speaker'] = 'speaker_2'
        before = store.update(identifier, segments=rows)
        endpoint = f'/api/projects/{identifier}'
        base = client.get(endpoint+'/runtime').json()['content_version']
        assert client.post(endpoint+'/run/retranslate_all').status_code == 200
        future = app.state.pipeline.futures.get(identifier)
        try:
            assert second_batch.wait(5)
            runtime = client.get(endpoint+'/runtime').json()
            assert runtime['content_version'] == base and runtime['translation_preview']['done'] == 1
            partial = client.get(endpoint+'/translation-proposal').json()
            assert partial['partial'] and [item['id'] for item in partial['items']] == [1]
            assert store.read(identifier)['segments'] == before['segments']
        finally:
            release_batch.set()
        if future:
            future.result(timeout=5)
        project = store.read(identifier)
        assert project['status'] == 'done'
        assert project['segments'] == before['segments'] and project['outputs'] == before['outputs']
        assert project['edit_revision'] == before['edit_revision']
        assert project['translation_proposal']['kind'] == 'retranslate_all'
        proposal = client.get(endpoint+'/translation-proposal').json()
        assert not proposal['stale'] and calls == [1, 2, 3]
        assert proposal['id'] == partial['id'] and not proposal.get('partial') and proposal['allow_edits']
        assert proposal['items'][0]['before'] == before['segments'][0]['zh']
        assert proposal['items'][2]['before'] == '' and proposal['items'][2]['after'] == '新译文3'
        request=dict(proposal_id=proposal['id'],revision=proposal['base_revision'],ids=[3])
        assert client.post(endpoint+'/translation-proposal/apply',json={**request,'edits':{1:'不在选择内'}}).status_code==409
        assert client.post(endpoint+'/translation-proposal/apply',json={**request,'edits':{3:'   '}}).status_code==409
        result = client.post(endpoint+'/translation-proposal/apply',json={**request,'edits':{3:' 人工改写的译文。 '}})
        assert result.status_code == 200 and result.json()['segments'][2]['zh'] == '人工改写的译文。'
        assert result.json()['segments'][:2] == before['segments'][:2]


def proposal(app,ident):
    st=app.state.store;p=st.read(ident)
    items=[dict(id=1,source=p["segments"][0]["text"],before=p["segments"][0]["zh"],after="完成12步后，模型才会收敛。",reason="恢复强调语序",estimated_seconds=3.2,seconds=10)]
    data=dict(id="a"*24,base_revision=p["edit_revision"],base_signature=digest(p["segments"]),items=items,total=2,changed=1)
    atomic_json(st.folder(ident)/"proposals"/("a"*24+".json"),data)
    st.update(ident,translation_proposal=dict(id="a"*24,status="ready",total=2,changed=1))
    return dict(proposal_id="a"*24,revision=p["edit_revision"],ids=[1])

def test_proposal_api_applies_only_selected_and_backs_up_media(tmp_path):
    app=create_app(tmp_path/"projects")
    with TestClient(app) as client:
        st,ident=setup_project(app);payload=proposal(app,ident)
        before=st.read(ident)
        get=client.get(f"/api/projects/{ident}/translation-proposal").json()
        assert not get["stale"] and st.read(ident)["outputs"]==before["outputs"]
        response=client.post(f"/api/projects/{ident}/translation-proposal/apply",json=payload)
        assert response.status_code==200
        after=response.json()
        assert after["segments"][0]["zh"]=="完成12步后，模型才会收敛。"
        assert after["segments"][1]==before["segments"][1] and after["outputs"]=={} and after["dub_signature"]==""
        assert (st.folder(ident)/"result.mkv").read_bytes()==b"old video"
        assert list((st.folder(ident)/"history").glob("*/result.mkv"))
        assert client.post(f"/api/projects/{ident}/translation-proposal/apply",json=payload).status_code==409

def test_stale_unknown_and_busy_proposal_cannot_overwrite(tmp_path):
    app=create_app(tmp_path/"projects")
    with TestClient(app) as client:
        st,ident=setup_project(app);payload=proposal(app,ident);url=f"/api/projects/{ident}/translation-proposal/apply"
        assert client.post(url,json={**payload,"ids":[2]}).status_code==409
        app.state.pipeline.events[ident]=threading.Event()
        try: assert client.post(url,json=payload).status_code==409
        finally: app.state.pipeline.events.pop(ident)
        p=st.read(ident);p["segments"][1]["zh"]="人工校对";st.update(ident,segments=p["segments"])
        assert client.get(f"/api/projects/{ident}/translation-proposal").json()["stale"]
        assert client.post(url,json=payload).status_code==409
        assert st.read(ident)["segments"][1]["zh"]=="人工校对"

def test_retranslation_keeps_unchanged_candidates_after_local_timeline_edits(tmp_path):
    app=create_app(tmp_path/"projects")
    with TestClient(app) as client:
        store,identifier=setup_project(app)
        before=store.read(identifier)
        request=proposal(app,identifier)
        meta=store.read(identifier)['translation_proposal']
        store.update(identifier,translation_proposal={**meta,'kind':'retranslate_all'})
        folder=store.folder(identifier)
        path=folder/'proposals'/(meta['id']+'.json')
        original=json.loads(path.read_text(encoding='utf-8'))
        original['items'].append(dict(id=2,source='Hello.',before='你好。',after='大家好。'))
        atomic_json(path,original)
        atomic_json(folder/'segments.previous.json',before['segments'])
        segments=[dict(r) for r in before['segments']]
        segments[1]['end']-=1
        current=store.update(identifier,segments=segments)
        endpoint=f"/api/projects/{identifier}/translation-proposal"

        visible=client.get(endpoint).json()
        assert visible['id']==meta['id'] and visible['allow_edits'] and not visible['stale']
        assert visible['base_revision']==current['edit_revision']
        assert visible['needs_retranslation']==1 and [item['id'] for item in visible['items']]==[1]
        atomic_json(folder/'segments.previous.json',segments)
        assert client.get(endpoint).json()['items']==visible['items']
        assert client.post(endpoint+'/apply',json=request).status_code==409
        updated=client.post(endpoint+'/apply',json={**request,'revision':current['edit_revision'],'edits':{1:'完成12步后，就会收敛。'}})
        assert updated.status_code==200
        assert updated.json()['segments'][0]['zh']=='完成12步后，就会收敛。'
        assert updated.json()['segments'][1]==segments[1]


def test_failed_optimization_keeps_subtitles_and_completed_outputs(tmp_path,monkeypatch):
    app=create_app(tmp_path/"projects");st,ident=setup_project(app);before=st.read(ident)
    from contextlib import contextmanager
    @contextmanager
    def local(*_): yield "http://127.0.0.1"
    monkeypatch.setattr("app.translation_proposals.local_translator",local)
    monkeypatch.setattr("app.translation_proposals.run_workflow",lambda *_a,**_k: (_ for _ in ()).throw(RuntimeError("bad model")))
    try:
        with pytest.raises(RuntimeError,match="bad model"):
            generate_proposal(st,ident,Settings(),threading.Event(),lambda *_:None)
        after=st.read(ident)
        assert after["segments"]==before["segments"] and after["outputs"]==before["outputs"] and after["dub_signature"]=="keep"
    finally: app.state.pipeline.close()


def test_batch_local_ids_round_trip_sparse_global_ids_and_exclude_context_ids(monkeypatch):
    from app.translation_quality import _request
    calls=stub(monkeypatch,[[dict(id=2,zh="第二句。",reason="乙"),dict(id="1",zh="第一句。",reason="甲")]])
    rows=[row(37,text="First."),row(42,text="Second.")]
    context=[dict(row(36,text="Context."),position="before")]
    result=_request("http://127.0.0.1",Settings(),rows,context,"course",threading.Event(),"review",{37:"旧甲",42:"旧乙"})
    assert result=={37:{"zh":"第一句。","reason":"甲"},42:{"zh":"第二句。","reason":"乙"}}
    payload=json.loads(calls[0]["messages"][1]["content"].removesuffix("\n/no_think"))
    assert payload['target_ids']==[1,2]
    assert [s['id'] for s in payload['segments']]==[1,2]
    assert [s['draft'] for s in payload['segments']]==['旧甲','旧乙']
    assert 'id' not in payload['context'][0]
    assert rows[0]['id']==37 and context[0]['id']==36


def test_id_mismatch_repair_reports_exact_missing_and_extra_ids(monkeypatch):
    from app.translation_quality import _request
    calls=stub(monkeypatch,[[dict(id=77,zh='错号')],[dict(id=1,zh='正确')]])
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    result=_request('http://127.0.0.1',Settings(),[row(77,text='Hello.')],[],'course',threading.Event(),'draft')
    assert result[77]['zh']=='正确'
    repair=calls[1]['messages'][1]['content']
    assert '缺少编号 [1]' in repair and '多余编号 [77]' in repair and '要求编号为 [1]' in repair


def test_persistent_id_mismatch_splits_without_guessing_order_and_caches_whole_result(tmp_path,monkeypatch):
    from app.translation_quality import _request
    calls=stub(monkeypatch,[[dict(id=99,zh='拒绝错号')]]*3+[[dict(id=1,zh='甲')],[dict(id=1,zh='乙')]])
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    rows=[row(7,text='A.'),row(15,text='B.')]
    args=('http://127.0.0.1',Settings(),rows,[],'course',threading.Event(),'draft')
    first=_request(*args,cache=tmp_path)
    assert first=={7:{'zh':'甲','reason':''},15:{'zh':'乙','reason':''}}
    assert len(calls)==5
    assert _request(*args,cache=tmp_path)==first and len(calls)==5


def test_failed_split_never_returns_or_caches_partial_parent_result(tmp_path,monkeypatch):
    from app.translation_quality import _request
    calls=stub(monkeypatch,[[dict(id=99,zh='错号')]]*3+[[dict(id=1,zh='甲')]]+[[dict(id=99,zh='错号')]]*3)
    monkeypatch.setattr(threading.Event,'wait',lambda *_:None)
    rows=[row(7,text='A.',zh='原甲'),row(15,text='B.',zh='原乙')]
    before=json.loads(json.dumps(rows))
    with pytest.raises(RuntimeError,match='缺少编号'):
        _request('http://127.0.0.1',Settings(),rows,[],'course',threading.Event(),'draft',cache=tmp_path)
    assert rows==before and len(calls)==7
    cached=[json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.glob('translation-*.json')]
    assert len(cached)==1 and set(cached[0])=={'7'}


@pytest.mark.parametrize('bad_id',[True,False,1.0,1.9,None,'1.0','1e0'])
def test_translation_rejects_coerced_identifiers(bad_id):
    from app.translation import parse_translation,TranslationIDsError
    with pytest.raises(TranslationIDsError):
        parse_translation(json.dumps({'translations':[dict(id=bad_id,zh='文本')]}),[1])


def test_applying_selected_proposal_preserves_its_review_checks(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st,ident=setup_project(app);payload=proposal(app,ident)
        path=st.folder(ident)/'proposals'/('a'*24+'.json')
        data=json.loads(path.read_text(encoding='utf-8'))
        data['items'][0]['checks']=[dict(code='review_rejected',message='审校结果未通过校验，已保留此前通过校验的译文。')]
        atomic_json(path,data)
        response=client.post(f'/api/projects/{ident}/translation-proposal/apply',json=payload)
        assert response.status_code==200
        p=response.json();check=p['translation_checks']['1']
        assert check['translation']==p['segments'][0]['zh'] and check['source']==p['segments'][0]['text']
        assert check['issues'][0]['code']=='review_rejected'
