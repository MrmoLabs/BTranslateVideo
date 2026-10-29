import copy
import json
import shutil
import threading
import time
import wave
from pathlib import Path

import pytest

from app.alignment import plan_alignment
from app.config import Settings
from app.media import Cancelled, fit_clip, wav_duration
from app.pipeline import Pipeline
from app.store import Store


def rows(*times):
    return [dict(id=i+1, start=a, end=b, text="Source", zh="中文", speaker="speaker_1") for i, (a,b) in enumerate(times)]


def test_short_first_utterance_borrows_time_without_losing_words_or_overlap():
    source = rows((0, 2.74), (2.74, 7.76), (7.76, 16.46))
    before = copy.deepcopy(source)
    durations = [3.95, 5.78, 7.42]
    settings = Settings()
    plan = plan_alignment(source, durations, 18, settings)
    assert plan[0]["window"] > 2.74
    assert plan[1]["start"] > 2.74
    for row, duration, slot in zip(source, durations, plan):
        assert row["start"] - settings.max_lead <= slot["start"] <= row["start"] + settings.max_lag
        assert duration / slot["window"] <= settings.max_speed
    assert all(a["start"] + min(duration, a["window"]) <= b["start"] + .00001 for a, b, duration in zip(plan, plan[1:], durations))
    assert source == before


def test_zero_drift_can_be_requested_and_impossible_end_is_rejected():
    with pytest.raises(ValueError, match="不会截断"):
        plan_alignment(rows((0, 2.74), (2.74, 7.76)), [3.95, 5.78], 8, Settings(max_lag=0))
    with pytest.raises(ValueError, match="不会截断"):
        plan_alignment(rows((0, 1)), [4], 1, Settings())


def test_original_overlapping_dialogue_keeps_its_mix():
    plan = plan_alignment(rows((0, 3), (1, 4)), [2, 2], 5, Settings())
    assert [slot["start"] for slot in plan] == [0, 1]


def test_time_planning_rejects_invalid_duration():
    with pytest.raises(ValueError):
        plan_alignment(rows((0, 1)), [0], 2, Settings())


def write_wav(path, duration=.8, rate=24000):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * round(duration * rate))


def test_ready_pcm_copy_preserves_every_sample_without_ffmpeg(tmp_path, monkeypatch):
    source, target = tmp_path / "source.wav", tmp_path / "target.wav"
    write_wav(source)
    monkeypatch.setattr("app.media.ffmpeg", lambda *_: pytest.fail("ready PCM should not be re-encoded"))
    assert fit_clip(source, target, 1, Settings(), tmp_path / "log", threading.Event()) == 1
    assert source.read_bytes() == target.read_bytes()


def test_parallel_fits_keep_source_order_deduplicate_tts_and_reuse_cache(tmp_path, monkeypatch):
    settings = Settings(audio_workers=3)
    store = Store(tmp_path / "projects"); p = store.create("parallel", "en", "course"); folder = store.folder(p["id"])
    (folder / "cache").mkdir(); (folder / "reference.wav").write_bytes(b"reference")
    model = tmp_path / "model"; model.mkdir(); (model / "config.yaml").write_text("test: true", encoding="utf-8")
    settings.index_model = str(model)
    source = rows(*[(i, i+1) for i in range(8)])
    store.update(p["id"], segments=source, media={"duration": 10}, references={"speaker_1": "reference.wav"})
    monkeypatch.setattr("app.pipeline.index_python", lambda _: Path(__file__))
    batches = []
    def tts(args, *_a, **_kw):
        request = json.loads(Path(args[-1]).read_text(encoding="utf-8")); batches.append(len(request["clips"]))
        for clip in request["clips"]: write_wav(clip["output"])
    monkeypatch.setattr("app.pipeline.run", tts)
    counters = dict(active=0, peak=0, calls=0); lock = threading.Lock()
    def fit(source, destination, window, *_):
        with lock:
            counters["active"] += 1; counters["calls"] += 1; counters["peak"] = max(counters["peak"], counters["active"])
        try:
            time.sleep(.03); shutil.copyfile(source, destination)
        finally:
            with lock: counters["active"] -= 1
    monkeypatch.setattr("app.pipeline.fit_clip", fit)
    pipeline = Pipeline(store)
    try:
        pipeline.dub(p["id"], settings, threading.Event())
        assert batches == [1]  # Eight identical texts share one synthesis job.
        assert counters["peak"] == 3
        assert [x["id"] for x in store.read(p["id"])["dub_timing"]] == list(range(1,9))
        assert wav_duration(folder / "chinese.wav") == 10
        pipeline.dub(p["id"], settings, threading.Event())
        assert counters["calls"] == 8 and batches == [1]
        saved = (folder / "chinese.wav").read_bytes(); cancel = threading.Event(); cancel.set()
        with pytest.raises(Cancelled): pipeline.dub(p["id"], settings, cancel)
        assert (folder / "chinese.wav").read_bytes() == saved
    finally:
        pipeline.close()
