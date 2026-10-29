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
    for name in ["chinese.srt", "chinese.wav", "result.mkv"]:
        (folder / name).write_bytes(b"previous output")
    old = [dict(id=1, start=0, end=2, text="hello there", zh="人工译文", speaker="speaker_1")]
    store.update(identifier, segments=old, media={"duration": 5}, outputs={"video": "result.mkv"}, dub_signature="old")
    pipeline = Pipeline(store)
    monkeypatch.setattr(pipeline, "audio", lambda *_: folder / "fake.wav")
    monkeypatch.setattr("app.pipeline.run", lambda *_a, **_kw: atomic_json(folder / "cache/asr.json", raw_words("hello there")))
    @contextmanager
    def translator(*_):
        yield "unused"
    monkeypatch.setattr("app.pipeline.local_translator", translator)
    def fail(*_):
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
        monkeypatch.setattr("app.pipeline.semantic_segments", lambda *_: new)
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
    assert VERSION == 9
    assert len(list(tmp_path.glob("sentences-*.json"))) == 2


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
