import json
import threading
import wave
from pathlib import Path

import pytest

from app.config import Settings, atomic_json
from app.media import Cancelled, mix_timeline
from app.store import Store
from app.subtitles import merge_short_segments, parse_srt, render_srt, validate_segments
from app.translation import parse_translation


def segment(**values):
    return dict(id=1, start=0, end=3, text="Hello world.", zh="你好，世界。", speaker="speaker_1", **values)


def test_srt_bom_roundtrip_and_invalid_times():
    data = "\ufeff1\r\n00:00:01,005 --> 00:00:03,010\r\nHello\r\nworld\r\n\r\n2\r\n00:00:04.000 --> 00:00:06.000\r\n日本語\r\n"
    result = parse_srt(data, 8)
    assert result[0]["start"] == 1.005
    assert result[0]["text"] == "Hello\nworld"
    assert parse_srt(render_srt(result, "text"), 8) == result
    with pytest.raises(ValueError):
        parse_srt("1\n00:99:00,000 --> 00:99:03,000\nhello")
    with pytest.raises(ValueError):
        validate_segments([segment() | {"end": float("nan")}])
    with pytest.raises(ValueError):
        validate_segments([segment(), segment()])


def test_merge_preserves_speaker_changes_and_overlap():
    rows = [segment() | {"text": "first", "end": 1}, segment() | {"id": 2, "start": 1.1, "end": 2, "text": "second"}, segment() | {"id": 3, "start": 1.9, "end": 4, "speaker": "speaker_2"}]
    merged = merge_short_segments(rows)
    assert len(merged) == 2
    assert merged[0]["text"] == "first second"
    assert merged[1]["speaker"] == "speaker_2"


def test_translation_ids_prevent_silent_misalignment():
    assert parse_translation('```json\n{"translations":[{"id":2,"zh":"乙"},{"id":1,"zh":"甲"}]}\n```', [1, 2]) == {1: "甲", 2: "乙"}
    for content in [
        '{"translations":[{"id":1,"zh":"甲"}]}',
        '{"translations":[{"id":1,"zh":"甲"},{"id":1,"zh":"乙"}]}',
        '{"translations":[{"id":1,"zh":""},{"id":2,"zh":"乙"}]}',
    ]:
        with pytest.raises(ValueError):
            parse_translation(content, [1, 2])


def test_local_only_settings():
    for url in ["https://example.com/v1", "http://127.0.0.1.evil.com/v1", "http://user:password@localhost/v1"]:
        with pytest.raises(ValueError):
            Settings(translation_url=url)
    assert Settings(translation_url="http://localhost:1234/v1/").translation_url == "http://localhost:1234/v1"


def test_atomic_utf8_and_crash_recovery(tmp_path):
    target = tmp_path / "测试.json"
    atomic_json(target, {"内容": "日本語和中文"})
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")
    store = Store(tmp_path / "projects")
    project = store.create("课程", "en", "course")
    store.update(project["id"], status="running")
    store.recover()
    assert store.read(project["id"])["status"] == "interrupted"
    with pytest.raises(ValueError):
        store.folder("../outside")


def write_wave(path, samples, rate=24000):
    import numpy as np
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def test_timeline_preserves_silence_and_overlapping_speakers(tmp_path):
    import numpy as np
    a, b, out = [tmp_path / name for name in ("a.wav", "b.wav", "mix.wav")]
    write_wave(a, np.full(24000, 1000))
    write_wave(b, np.full(24000, 2000))
    mix_timeline([(1, a), (1.5, b)], 3, out, threading.Event())
    with wave.open(str(out), "rb") as stream:
        assert stream.getnframes() == 72000
        values = np.frombuffer(stream.readframes(72000), dtype="<i2")
    assert np.all(values[:24000] == 0)
    assert np.all(values[24000:36000] == 1000)
    assert np.all(values[36000:48000] == 3000)
    assert np.all(values[48000:60000] == 2000)
    assert np.all(values[60000:] == 0)


def test_timeline_cancellation_cleans_scratch(tmp_path):
    event = threading.Event()
    event.set()
    out = tmp_path / "mix.wav"
    with pytest.raises(Cancelled):
        mix_timeline([], 2, out, event)
    assert not out.with_suffix(".mix.tmp").exists()


def test_translation_request_cancels_without_waiting_for_model(monkeypatch):
    import asyncio
    import time
    import httpx
    from app.translation import _post_cancellable

    original = httpx.AsyncClient
    async def delayed(request):
        await asyncio.sleep(20)
        return httpx.Response(200, json={})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(delayed)))
    event = threading.Event()
    timer = threading.Timer(.08, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            asyncio.run(_post_cancellable("http://127.0.0.1/model", {}, event))
        assert time.monotonic() - started < 2
    finally:
        timer.cancel()


def test_model_worker_rejects_network_connections(monkeypatch):
    import socket
    from scripts.model_worker import block_network
    with monkeypatch.context() as context:
        context.setattr(socket.socket, "connect", socket.socket.connect)
        block_network()
        with socket.socket() as connection:
            with pytest.raises(RuntimeError, match="禁止联网"):
                connection.connect(("127.0.0.1", 9))


def test_alignment_can_use_silence_without_overlapping_previous_speech():
    from app.pipeline import place_start
    settings = Settings(max_speed=1.35, max_lead=.75)
    shifted = place_start(8.85, 10, 1.63, 5, settings)
    assert 8.1 <= shifted < 8.85
    assert 1.63 / (10 - shifted) <= settings.max_speed
    assert place_start(8.85, 10, 1.63, 9, settings) == 8.85
    assert place_start(8.85, 10, 1, 5, settings) == 8.85
