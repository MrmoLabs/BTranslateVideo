import json
import shutil
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.media import extract_audio, ffmpeg, fit_clip, probe, wav_duration
from app.pipeline import Pipeline
from app.store import Store


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_media_export_and_voice_cache_invalidation(tmp_path, monkeypatch):
    settings = Settings(max_speed=1.35, preview_encoder="cpu", tts_idle_minutes=0)
    store = Store(tmp_path / "projects")
    project = store.create("integration", "en", "course")
    identifier = project["id"]
    folder = store.folder(identifier)
    (folder / "cache").mkdir()
    cancel = threading.Event()
    log = folder / "process.log"
    ffmpeg(settings, ["-f", "lavfi", "-i", "color=c=navy:s=320x180:r=24:d=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-f", "matroska", str(folder / "source.media")], log, cancel)
    info = probe(folder / "source.media", settings)
    rows = [{"id": 1, "start": 1, "end": 4, "text": "One", "zh": "第一句", "speaker": "speaker_1"}, {"id": 2, "start": 5, "end": 7, "text": "Two", "zh": "第二句", "speaker": "speaker_1"}]
    extract_audio(folder / "source.media", folder / "reference.wav", settings, log, cancel, duration=4)
    store.update(identifier, segments=rows, media=info, references={"speaker_1": "reference.wav"}, background_mode="none")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.yaml").write_text("test: true", encoding="utf-8")
    settings.index_model = str(model)
    monkeypatch.setattr("app.pipeline.index_python", lambda _: Path(__file__))
    calls = []

    def fake_tts(args, *_a, **_kw):
        # Test doubles only model synthesis; all audio fit/mix/mux operations are real.
        request = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        calls.append(len(request["clips"]))
        for clip in request["clips"]:
            with wave.open(clip["output"], "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                values = (np.sin(np.arange(24000) * .04) * 4000).astype("<i2")
                stream.writeframes(values.tobytes())

    monkeypatch.setattr("app.pipeline.run", fake_tts)
    pipeline = Pipeline(store)
    try:
        pipeline.dub(identifier, settings, cancel)
        pipeline.dub(identifier, settings, cancel)
        assert calls == [2]
        rows[1]["zh"] = "第二句话已修改"
        store.update(identifier, segments=rows, dub_signature="", outputs={})
        pipeline.dub(identifier, settings, cancel)
        assert calls == [2, 1]
        assert abs(wav_duration(folder / "chinese.wav") - info["duration"]) < .001
        pipeline.export(identifier, settings, cancel)
        result = probe(folder / "result.mkv", settings)
        audio = [s for s in result["streams"] if s["codec_type"] == "audio"]
        subtitles = [s for s in result["streams"] if s["codec_type"] == "subtitle"]
        assert len(audio) == 2 and len(subtitles) == 1
        assert audio[1]["tags"]["language"] == "zho"
        assert audio[1]["disposition"]["default"] == 1
        assert abs(result["duration"] - info["duration"]) < .15
        assert not (folder / "bilingual.srt").read_bytes().startswith(b"\xef\xbb\xbf")
        settings.max_speed = 1.5
        with pytest.raises(ValueError, match="已变化"):
            pipeline.export(identifier, settings, cancel)
    finally:
        pipeline.close()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_overlong_dub_is_rejected_instead_of_truncated(tmp_path):
    source = tmp_path / "long.wav"
    settings = Settings()
    log = tmp_path / "ffmpeg.log"
    cancel = threading.Event()
    ffmpeg(settings, ["-f", "lavfi", "-i", "sine=duration=3", str(source)], log, cancel)
    with pytest.raises(ValueError, match="不会截断"):
        fit_clip(source, tmp_path / "fit.wav", 1, settings, log, cancel)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_atempo_fitting_measures_actual_output_without_tail_trimming(tmp_path):
    settings = Settings()
    log = tmp_path / "ffmpeg.log"
    source, target = tmp_path / "source.wav", tmp_path / "fit.wav"
    cancel = threading.Event()
    ffmpeg(settings, ["-f", "lavfi", "-i", "sine=duration=6.942766", "-ar", "22050", str(source)], log, cancel)
    speed = fit_clip(source, target, 6.16, settings, log, cancel)
    assert 1 < speed <= settings.max_speed
    assert wav_duration(target) <= 6.16 + 1 / 24000
