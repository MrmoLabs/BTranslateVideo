import json
from contextlib import contextmanager
from threading import Event

import pytest

from app.config import Settings, atomic_json
from app.pipeline import Pipeline
from app.segmentation import flatten_words, lexical, parse_sentences, semantic_segments
from app.store import Store


def raw_words(text, step=.5):
    words = [dict(word=" " + word, start=i * step, end=(i + 1) * step) for i, word in enumerate(text.split())]
    return [dict(text=text, words=words, speaker="speaker_1")]


def test_exact_source_words_and_punctuation_are_preserved():
    raw = raw_words("let's create our first file it is our model")
    words = flatten_words(raw, 20)
    cuts = parse_sentences(json.dumps({"sentences": ["Let's create our first file.", "It is our model."]}), words)
    assert [cut["end"] for cut in cuts] == [5, 9]
    assert cuts[0]["text"] == "Let's create our first file."
    for sentences in [["Let's create the first file. It is our model."], ["Let's create our first file."]]:
        with pytest.raises(ValueError, match="增删"):
            parse_sentences(json.dumps({"sentences": sentences}), words)


@pytest.mark.parametrize("text,sentences", [
    ("create our first file", ["Create our.", "First file."]),
    ("build input embeddings now", ["Build input.", "Embeddings now."]),
    ("okay build this model", ["Okay.", "Build this model."]),
])
def test_dangling_phrases_and_fillers_are_not_utterances(text, sentences):
    with pytest.raises(ValueError):
        parse_sentences(json.dumps({"sentences": sentences}), flatten_words(raw_words(text), 20))


def test_long_window_carries_last_unit_into_next_block(tmp_path, monkeypatch):
    raw = raw_words(" ".join(f"w{i}" for i in range(300)), .1)
    windows = []
    def planner(_url, _settings, words, *_args):
        windows.append([word["word"].strip() for word in words])
        ends = [100, len(words)] if len(words) > 200 else [len(words)]
        return [dict(end=end, punct=".") for end in ends]
    monkeypatch.setattr("app.segmentation.plan_block", planner)
    segments = semantic_segments(raw, 30, "unused", Settings(), "course", Event(), tmp_path)
    assert windows[0][-1] == "w239"
    assert windows[1][0] == "w100"
    assert len(segments) == 2
    assert lexical("".join(s["text"] for s in segments)) == lexical(raw[0]["text"])


def test_final_pass_joins_a_phrase_split_between_model_windows(tmp_path,monkeypatch):
    prefix=' '.join([f'w{i}' for i in range(237)]+['end.'])
    raw=raw_words(prefix+' we could create a situation.',step=.2)
    planner=lambda _url,_settings,words,*_: [dict(end=len(words),punct='.')]
    monkeypatch.setattr('app.segmentation.plan_block',planner)
    rows=semantic_segments(raw,60,'unused',Settings(),'course',Event(),tmp_path)
    assert [r['text'] for r in rows]==[prefix.capitalize(),'We could create a situation.']
    assert rows[1]['start']==raw[0]['words'][238]['start']
    assert rows[1]['end']==raw[0]['words'][-1]['end']


def test_only_continuous_in_sentence_cuts_are_sent_for_automatic_review(tmp_path,monkeypatch):
    import httpx

    raw=raw_words('The presenter handed the microphone to the student. We moved on.',step=.3)
    monkeypatch.setattr('app.segmentation.plan_block',lambda *_:[
        dict(end=3,punct='.'),dict(end=8,punct='.'),dict(end=11,punct='.')])
    # A short beat before the object makes this an ambiguous review case.
    raw[0]['words'][2]['end']-=.18
    for word in raw[0]['words'][8:]:
        word['start']+=2;word['end']+=2
    seen=[]

    async def post(url,payload,cancel,**_):
        data=json.loads(payload['messages'][-1]['content'].removesuffix('\n/no_think'))
        seen.append(data)
        return httpx.Response(200,request=httpx.Request('POST',url),
                              json={'choices':[{'message':{'content':'{"decisions":["join"]}'}}]})

    monkeypatch.setattr('app.segmentation._post_cancellable',post)
    args=(raw,30,'http://127.0.0.1/v1',Settings(),'course',Event(),tmp_path)
    rows=semantic_segments(*args)
    assert seen==[[{'left':'The presenter handed','right':'the microphone to the student.'}]]
    assert [r['text'] for r in rows]==['The presenter handed the microphone to the student.','We moved on.']
    assert rows[1]['start']==raw[0]['words'][8]['start']
    assert semantic_segments(*args)==rows and len(seen)==1


def test_short_asr_sentences_restore_real_boundaries_instead_of_joining_everything(tmp_path,monkeypatch):
    from app.segmentation import repair_sentence_breaks

    raw=raw_words('You actually spend time thinking about it. These are the main examples. That is all.',step=.25)
    words=flatten_words(raw,10)
    ends=[3,8,len(words)]
    groups=[('You actually spend',0),('time thinking about it. These',3),('are the main examples. That is all.',8)]
    rows=[dict(id=i,start=words[start]['start'],end=words[end-1]['end'],speaker='speaker_1',zh='',text=text)
          for i,((text,start),end) in enumerate(zip(groups,ends),1)]
    monkeypatch.setattr('app.segmentation.review_sentence_breaks',lambda *_:pytest.fail('short uninterrupted sentences can be fixed from ASR'))
    result=repair_sentence_breaks(words,rows,ends,'unused',Settings(),Event(),tmp_path)
    assert [r['text'] for r in result]==['You actually spend time thinking about it.','These are the main examples.','That is all.']
    assert [r['end'] for r in result]==[words[index]['end'] for index in (6,11,len(words)-1)]
    assert lexical(''.join(r['text'] for r in result))==lexical(raw[0]['text'])


def test_failed_review_is_split_without_counting_unanswered_cuts_as_reviewed(tmp_path,monkeypatch):
    from app.segmentation import reviewed_boundary_batch

    seen=[]
    def review(_url,_settings,items,_cancel):
        seen.append(len(items))
        if len(items)>4:
            raise ValueError('wrong number of decisions')
        if items[0]['id']==5:
            raise ValueError('still malformed')
        return {items[0]['id']}

    monkeypatch.setattr('app.segmentation.review_sentence_breaks',review)
    batch=[dict(id=i,left='a complete left',right='continues here') for i in range(1,9)]
    joined,unreviewed=reviewed_boundary_batch('unused',Settings(),batch,Event(),tmp_path)
    assert joined=={1} and unreviewed==4 and seen==[8,4,4]


def test_numbered_grid_is_rejected_even_with_valid_word_ids():
    from app.segmentation import mechanical_numbered_plan, source_punctuation_plan

    raw=raw_words(' '.join(['The speaker explains the topic to everyone.']*5),step=.3)
    words=flatten_words(raw,30)
    cuts=[dict(end=i,punct='.') for i in range(2,len(words),2)]+[dict(end=len(words),punct='.')]
    assert mechanical_numbered_plan(cuts,words)
    source=source_punctuation_plan(words,'course')
    assert [cut['end'] for cut in source]==[7,14,21,28,35]
    assert not mechanical_numbered_plan(source,words)


def test_continuation_drops_invented_period_and_real_question_is_restored(tmp_path):
    from app.segmentation import audit_boundary_checks, reconcile_punctuation

    raw=raw_words('Does everyone know what a cold call is? They nod.',step=.3)
    words=flatten_words(raw,10)
    ends=[3,8,10]
    rows=[
        dict(id=i,start=words[start]['start'],end=words[end-1]['end'],speaker='speaker_1',text=text)
        for i,(start,end,text) in enumerate(zip([0,3,8],ends,['Does everyone know.','What a cold call is.','They nod.']),1)
    ]
    corrected=reconcile_punctuation(words,rows,ends)
    assert [row['text'] for row in corrected]==['Does everyone know','what a cold call is?','They nod.']
    assert lexical(''.join(row['text'] for row in corrected))==lexical(raw[0]['text'])
    path=tmp_path/'asr.json';atomic_json(path,raw)
    warnings=audit_boundary_checks(rows,path,10)
    assert warnings['1']['issues'][0]['code']=='asr_sentence_split'
    assert warnings['2']['issues']==[{'code':'asr_terminal_punctuation','punctuation':'?'}]


def test_clipped_final_phrase_merges_with_previous_thought(tmp_path, monkeypatch):
    raw = raw_words("set model dimension the dimension of the")
    monkeypatch.setattr("app.segmentation.plan_block", lambda *_: [dict(end=3, punct="."), dict(end=7, punct=".")])
    segments = semantic_segments(raw, 4, "unused", Settings(), "course", Event(), tmp_path)
    assert len(segments) == 1
    assert segments[0]["text"].endswith("…")
    assert segments[0]["end"] == 3.5


def test_speaker_change_keeps_utterances_separate(tmp_path, monkeypatch):
    raw = raw_words("hello there")
    raw += [dict(text="yes", words=[dict(word=" yes", start=1, end=1.5)], speaker="speaker_2")]
    monkeypatch.setattr("app.segmentation.plan_block", lambda _u, _s, words, *_: [dict(end=len(words), punct=".")])
    segments = semantic_segments(raw, 2, "unused", Settings(), "anime", Event(), tmp_path)
    assert [s["speaker"] for s in segments] == ["speaker_1", "speaker_2"]


def test_failed_resegment_preserves_edits_then_success_backs_up(tmp_path, monkeypatch):
    store = Store(tmp_path / "projects")
    project = store.create("edited", "en", "course")
    identifier = project["id"]
    folder = store.folder(identifier)
    (folder / "cache").mkdir()
    (folder / "source.media").write_bytes(b"original video")
    from app.pipeline import file_hash
    atomic_json(folder/'cache/asr.json',raw_words('hello there'))
    atomic_json(folder/'cache/asr-words-metadata.json',dict(source=file_hash(folder/'source.media'),model='cached-whisper'))
    for name in ["chinese.srt", "chinese.wav", "result.mkv"]:
        (folder / name).write_bytes(b"previous output")
    old = [dict(id=1, start=0, end=2, text="hello there", zh="人工译文", speaker="speaker_1")]
    store.update(identifier, segments=old, media={"duration": 5}, outputs={"video": "result.mkv"}, dub_signature="old")
    pipeline = Pipeline(store)
    monkeypatch.setattr(pipeline, "audio", lambda *_: folder / "fake.wav")
    monkeypatch.setattr("app.model_backends.run", lambda *_a, **_kw: atomic_json(folder / "cache/asr.json", raw_words("hello there")))
    @contextmanager
    def translator(*_):
        yield "unused"
    monkeypatch.setattr("app.pipeline.local_translator", translator)
    def fail(*_,**_options):
        raise ValueError("bad plan")
    monkeypatch.setattr("app.pipeline.semantic_segments", fail)
    try:
        with pytest.raises(ValueError, match="bad plan"):
            pipeline.analyze(identifier, Settings(), Event(), force=True)
        unchanged = store.read(identifier)
        assert unchanged["segments"] == old
        assert unchanged["outputs"] == {"video": "result.mkv"}
        assert not (folder / "history").exists()
        new = [dict(old[0], text="Hello there.", zh="")]
        monkeypatch.setattr("app.pipeline.semantic_segments", lambda *_,**_options: new)
        pipeline.analyze(identifier, Settings(), Event(), force=True)
        updated = store.read(identifier)
        assert updated["segments"] == new
        assert updated["outputs"] == {}
        backups = list((folder / "history").iterdir())
        assert len(backups) == 1
        backup = json.loads((backups[0] / "project.json").read_text(encoding="utf-8"))
        assert backup["segments"] == old
        assert (backups[0] / "result.mkv").read_bytes() == b"previous output"
        assert not (folder / "project.json").read_bytes().startswith(b"\xef\xbb\xbf")
    finally:
        pipeline.close()


def test_long_coding_pauses_split_at_sentences_not_inside_phrases():
    raw = raw_words("create our class this is the constructor", .4)
    words = flatten_words(raw, 20)
    for word in words[4:]:
        word["start"] += 8
        word["end"] += 8
    cuts = parse_sentences(json.dumps({"sentences": ["Create our class. This is the constructor."]}), words)
    assert [x["end"] for x in cuts] == [3, 7]
    assert cuts[1]["text"] == "This is the constructor."
    # The actual silence lies between 'this' and 'is', not at the cut.
    assert cuts[0]["end"] != 4


def test_long_pause_also_separates_restored_comma_between_independent_clauses():
    words = flatten_words(raw_words("this is the constructor we need its dimension", .4), 20)
    for word in words[1:]:
        word["start"] += 8
        word["end"] += 8
    cuts = parse_sentences(json.dumps({"sentences": ["This is the constructor, We need its dimension."]}), words)
    assert [x["end"] for x in cuts] == [4, 8]


def test_long_clipped_clause_does_not_swallow_previous_complete_sentence(tmp_path, monkeypatch):
    raw = raw_words("this is the constructor we need to tell it the dimension of the model so the dimension of the", .4)
    monkeypatch.setattr("app.segmentation.plan_block", lambda _u, _s, words, *_: [dict(end=4, punct="."), dict(end=len(words), punct=".")])
    segments = semantic_segments(raw, 20, "unused", Settings(), "course", Event(), tmp_path)
    assert len(segments) == 2
    assert segments[0]["text"] == "This is the constructor."
    assert segments[1]["text"].endswith("…")


def test_model_cleanup_of_repetitions_only_transfers_boundaries():
    from app.segmentation import parse_proposals
    raw = raw_words("usually the the additive is called bias it is always added")
    words = flatten_words(raw, 20)
    cuts = parse_proposals(json.dumps({"sentences": ["Usually the additive is called bias.", "It is always added."]}), words)
    assert cuts[0]["text"] == "Usually the the additive is called bias."
    assert lexical("".join(x["text"] for x in cuts)) == lexical(raw[0]["text"])


def test_recovered_punctuation_does_not_duplicate_code_attribute_dot():
    from app.segmentation import parse_proposals
    raw = raw_words("the the model uses nn .parameter it is learnable")
    raw[0]["words"][5]["word"] = ".parameter"
    raw[0]["text"] = "".join(x["word"] for x in raw[0]["words"]).strip()
    words = flatten_words(raw, 20)
    cuts = parse_proposals(json.dumps({"sentences": ["The model uses nn.parameter.", "It is learnable."]}), words)
    assert "nn.parameter" in cuts[0]["text"]
    assert "nn..parameter" not in cuts[0]["text"]
    assert "the the" in cuts[0]["text"].lower()


def test_major_rewrite_cannot_be_used_as_boundary_proposals():
    from app.segmentation import parse_proposals
    words = flatten_words(raw_words("every detail of the formula must remain present"), 20)
    with pytest.raises(ValueError, match="改写过多"):
        parse_proposals(json.dumps({"sentences": ["A summary of this lesson."]}), words)


def test_boundary_protocol_rejects_cut_inside_code_identifier():
    from app.segmentation import parse_cuts
    words = flatten_words(raw_words("use nn .parameter now"), 20)
    words[2]["word"] = ".parameter"
    with pytest.raises(ValueError, match="代码标识符"):
        parse_cuts(json.dumps({"cuts": [{"end": 2}, {"end": 4}]}), words)


def test_planner_switches_to_boundary_only_after_unusable_rewrites(tmp_path, monkeypatch):
    import httpx
    from app.segmentation import plan_block
    calls = []
    async def response(_url, payload, _cancel):
        calls.append(payload)
        content = {"sentences": ["A completely different summary."]} if len(calls) <= 2 else {"cuts": [{"end": 4, "punct": "."}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}, request=httpx.Request("POST", _url))
    monkeypatch.setattr("app.segmentation._post_cancellable", response)
    words = flatten_words(raw_words("keep every original word"), 20)
    cuts = plan_block("http://127.0.0.1/v1", Settings(), words, "", "course", Event(), tmp_path)
    assert cuts == [{"end": 4, "punct": "."}]
    assert "last_word_id" in calls[2]["messages"][1]["content"]
    assert len(list(tmp_path.glob("attempt-*.json"))) == 2


def test_invalid_boundary_numbers_fall_back_to_original_punctuation(tmp_path,monkeypatch):
    import httpx
    from app.segmentation import plan_block,source_text

    raw=raw_words("It was hard. We could keep working.")
    words=flatten_words(raw,20)
    calls=[]
    async def response(url,payload,cancel):
        calls.append(payload)
        content=({"sentences":["The paragraph was polished."]} if len(calls)<=2
                 else {"cuts":[{"end":3,"punct":"."},{"end":len(words)+3,"punct":"."}]})
        return httpx.Response(200,request=httpx.Request("POST",url),json={
            "choices":[{"message":{"content":json.dumps(content)}}]})

    monkeypatch.setattr('app.segmentation._post_cancellable',response)
    result=plan_block("http://127.0.0.1/v1",Settings(),words,"","course",Event(),tmp_path)
    assert [part['end'] for part in result]==[3,len(words)] and len(calls)==3
    assert source_text(words[3:result[-1]['end']],result[-1]['punct'])=="We could keep working."
    fallback=json.loads((tmp_path/'fallback.json').read_text(encoding='utf-8'))
    assert fallback['words']==len(words) and '超出本批' in fallback['error']


def test_remote_request_failure_does_not_turn_into_successful_fallback(tmp_path,monkeypatch):
    import httpx
    from app.segmentation import plan_block

    async def response(url,payload,cancel):
        return httpx.Response(504,request=httpx.Request("POST",url))

    monkeypatch.setattr('app.segmentation._post_cancellable',response)
    with pytest.raises(RuntimeError,match='504'):
        plan_block("http://127.0.0.1/v1",Settings(),flatten_words(raw_words("This is a sentence."),20),"",
                   "course",Event(),tmp_path)
    assert not (tmp_path/'fallback.json').exists()


def test_existing_v6_cache_is_validated_and_reused(tmp_path, monkeypatch):
    import hashlib
    from app.segmentation import VERSION
    words = flatten_words(raw_words("keep all words"), 20)
    settings = Settings()
    key = hashlib.sha256(json.dumps([6, words, "course", settings.translation_model, settings.translation_name], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    atomic_json(tmp_path / ("sentences-" + key + ".json"), {"cuts": [{"end": 3, "punct": ".", "text": "Keep all words."}]})
    monkeypatch.setattr("app.segmentation.plan_block", lambda *_: pytest.fail("must reuse validated cache"))
    result = semantic_segments(raw_words("keep all words"), 20, "unused", settings, "course", Event(), tmp_path)
    assert result[0]["text"] == "Keep all words."
    assert VERSION >= 11
    assert len(list(tmp_path.glob("sentences-*.json"))) == 2


def test_context_is_read_only_and_sent_in_both_planner_protocols(monkeypatch):
    from app.segmentation import plan_block
    import httpx

    words=flatten_words(raw_words('check the logs'),5)
    seen=[]
    async def response(_url,payload,_cancel):
        seen.append(payload['messages'])
        content='{}' if len(seen)<3 else '{"cuts":[{"end":3,"punct":"."}]}'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]},request=httpx.Request('POST','http://testserver'))
    monkeypatch.setattr('app.segmentation._post_cancellable',response)
    cuts=plan_block('http://testserver',Settings(),words,{'before':'First restart it.','after':'Then check memory.'},'course',Event())
    assert cuts[-1]['end']==3
    for messages in (seen[0],seen[-1]):
        content=next(item['content'] for item in reversed(messages) if 'First restart it.' in item['content'])
        assert json.loads(content.split('\n/no_think')[0])['after']=='Then check memory.'


def test_chinese_condition_and_consequence_are_rejoined():
    from app.segmentation import parse_cuts
    raw=raw_words('如果 真有问题 就重启 服务')
    words=flatten_words(raw,5)
    plan='{"cuts":[{"end":2,"punct":"。"},{"end":4,"punct":"。"}]}'
    repaired=parse_cuts(plan,words,repair=True)
    assert [part['end'] for part in repaired]==[4]
    assert lexical(repaired[0]['text'])==lexical(raw[0]['text'])


@pytest.mark.parametrize(('before','after'),[
    ('What word did many', 'of us miss?'),
    ("Oh my goodness, I'm", 'nervous.'),
    ('And the same is true when', 'it comes to speaking.'),
    ("We didn't", 'get it right.'),
    ('Say the first thing that comes to', 'your mind.'),
    ('A question related to', 'communicating.'),
    ('You need', 'to have your anxiety under control.'),
    ('Some ground rules for', 'the interactivity today.'),
    ('And I invite you', 'to participate.'),
    ('85%', 'of people feel nervous.'),
    ('85', '% of people feel nervous.'),
    ('We could', 'create a situation.'),
    ('In', 'fact, that happens often.'),
    ('The study asked', 'Americans, what do you fear?'),
    ('What does', 'it feel like?'),
    ('Watching a nervous', 'speaker present?'),
])
def test_english_continuations_are_flagged_and_rejoined(before,after):
    from app.segmentation import EnglishContinuationError,boundary_checks,parse_cuts

    raw=raw_words(before+' '+after)
    words=flatten_words(raw,30)
    end=len(before.split())
    cut=json.dumps(dict(cuts=[dict(end=end),dict(end=len(words),punct=after[-1])]))
    with pytest.raises(EnglishContinuationError):
        parse_cuts(cut,words)
    result=parse_cuts(cut,words,repair=True)
    assert len(result)==1 and lexical(result[0]['text'])==lexical(raw[0]['text'])
    rows=[dict(id=1,speaker='A',start=0,end=1,text=before+'.'),
          dict(id=2,speaker='A',start=1,end=2,text=after)]
    assert boundary_checks(rows)['1']['issues']==[dict(code='english_continuation',next_id=2,next_source=after)]


def test_english_continuation_respects_source_punctuation_and_speaker():
    from app.segmentation import parse_cuts

    for source in ("No, we didn't. Get it right.", "No, we didn't get it right."):
        words=flatten_words(raw_words(source),30)
        if "didn't." not in source:
            for word in words[3:]:
                word['speaker']='other'
        cut=json.dumps(dict(cuts=[dict(end=3),dict(end=len(words))]))
        assert [part['end'] for part in parse_cuts(cut,words,repair=True)]==[3,len(words)]


def test_many_rejected_cuts_recover_source_sentence_boundaries():
    from app.segmentation import parse_cuts

    sentence='A surprisingly large share of people could speak at the event without any preparation.'
    raw=raw_words(' '.join([sentence]*8),step=.5)
    words=flatten_words(raw,80)
    size=len(sentence.split())
    mid=sentence.split().index('could')+1
    cuts=[dict(end=start+mid,punct='.') for start in range(0,len(words),size)]
    cuts.append(dict(end=len(words),punct='.'))
    repaired=parse_cuts(json.dumps({'cuts':cuts}),words,repair=True)
    assert [part['end'] for part in repaired]==list(range(size,len(words)+1,size))
    assert lexical(''.join(part['text'] for part in repaired))==lexical(raw[0]['text'])


def test_cached_english_boundary_is_repaired_without_calling_model(tmp_path,monkeypatch):
    from app.config import atomic_json

    raw=raw_words('What word did many of us miss?')
    settings=Settings()
    monkeypatch.setattr('app.segmentation.plan_block',lambda *_:[dict(end=7,punct='?')])
    semantic_segments(raw,30,'unused',settings,'course',Event(),tmp_path)
    [cache]=tmp_path.glob('sentences-*.json')
    atomic_json(cache,dict(cuts=[dict(end=4),dict(end=7,punct='?')]))
    monkeypatch.setattr('app.segmentation.plan_block',lambda *_:pytest.fail('Valid words can be rejoined from the cache'))
    rows=semantic_segments(raw,30,'unused',settings,'course',Event(),tmp_path)
    assert len(rows)==1 and rows[0]['text']=='What word did many of us miss?'


def test_local_segmentation_is_independent_of_the_translation_provider():
    from app.model_backends import uses_translation_service
    from app.segmentation import segmentation_settings

    remote = Settings(translation_provider='bilibili')
    assert segmentation_settings(remote) is remote
    requested = remote.model_copy(update={'segmentation_provider':'llama_cpp'})
    planner = segmentation_settings(requested)
    assert requested.translation_provider=='bilibili' and uses_translation_service(requested)
    assert planner.translation_provider=='llama_cpp' and not uses_translation_service(planner)
    assert planner.translation_model==remote.translation_model and planner.translation_json_mode
    assert segmentation_settings(planner) is planner


def test_minute_coding_pause_is_a_batch_boundary_after_complete_thought(tmp_path, monkeypatch):
    raw = raw_words("we do the same for the value okay now we calculate attention", .4)
    for word in raw[0]["words"][8:]:
        word["start"] += 52
        word["end"] += 52
    windows = []
    def planner(_u, _s, words, *_):
        windows.append([w["word"].strip() for w in words])
        return [dict(end=len(words), punct=".")]
    monkeypatch.setattr("app.segmentation.plan_block", planner)
    result = semantic_segments(raw, 60, "unused", Settings(), "course", Event(), tmp_path)
    assert len(result) == 2
    assert windows[0][-1] == "okay" and windows[1][0] == "now"
    assert result[1]["start"] > 50
    assert lexical("".join(s["text"] for s in result)) == lexical(raw[0]["text"])


def test_long_pause_does_not_force_cut_between_input_embeddings(tmp_path, monkeypatch):
    raw = raw_words("build input embeddings now", .4)
    for word in raw[0]["words"][2:]:
        word["start"] += 52
        word["end"] += 52
    from app.segmentation import parse_cuts
    calls = []
    def planner(_u, _s, words, *_):
        calls.append(words)
        return parse_cuts(json.dumps({"cuts": [{"end": len(words), "punct": "."}]}), words)
    monkeypatch.setattr("app.segmentation.plan_block", planner)
    result = semantic_segments(raw, 60, "unused", Settings(), "course", Event(), tmp_path)
    assert len(result) == 1 and len(calls) == 1
    assert "input embeddings" in result[0]["text"]


def test_obsolete_invalid_cached_cut_is_replanned(tmp_path, monkeypatch):
    import hashlib
    from app.segmentation import VERSION
    words = flatten_words(raw_words("keep all words"), 20)
    s = Settings()
    key = hashlib.sha256(json.dumps([VERSION, words, "course", s.translation_model, s.translation_name], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    atomic_json(tmp_path / ("sentences-" + key + ".json"), {"cuts": [{"end": 1}]})
    monkeypatch.setattr("app.segmentation.plan_block", lambda *_: [dict(end=3, punct=".")])
    result = semantic_segments(raw_words("keep all words"), 20, "unused", s, "course", Event(), tmp_path)
    assert result[0]["text"] == "Keep all words."


def test_bad_model_cut_is_coalesced_without_changing_any_words():
    from app.segmentation import parse_proposals
    raw = raw_words("let us create our first file it is our model")
    words = flatten_words(raw, 20)
    cuts = parse_proposals(json.dumps({"sentences": ["Let us create our.", "First file.", "It is our model."]}), words, repair=True)
    assert [x["end"] for x in cuts] == [6, 10]
    assert cuts[0]["text"] == "Let us create our first file."
    assert lexical("".join(c["text"] for c in cuts)) == lexical(raw[0]["text"])


def test_overlong_compound_uses_independent_clause_markers():
    text = "we can keep all the rest the default as in the paper if the model is too big for your GPU you can reduce the number of layers of course this will impact performance but I think this should not be a big problem"
    words = flatten_words(raw_words(text, 1), 100)
    cuts = parse_sentences(json.dumps({"sentences": [text + "."]}), words, repair=True)
    assert len(cuts) >= 3
    assert lexical("".join(c["text"] for c in cuts)) == lexical(text)


def test_complete_thought_is_not_cut_just_to_obey_soft_duration_target():
    from app.segmentation import parse_proposals
    text = "one complete explanation without a reliable independent clause boundary"
    words = flatten_words(raw_words(text, 5), 100)
    cuts = parse_proposals(json.dumps({"sentences": [text + "."]}), words, repair=True)
    assert len(cuts) == 1


def test_boundary_repair_does_not_hide_missing_source_coverage():
    from app.segmentation import parse_cuts
    words = flatten_words(raw_words("create our first file"), 20)
    with pytest.raises(ValueError, match="不能遗漏"):
        parse_cuts(json.dumps({"cuts": [{"end": 2, "punct": "."}]}), words, repair=True)
