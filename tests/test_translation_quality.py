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
