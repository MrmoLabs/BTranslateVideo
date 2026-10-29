import hashlib
import json
from pathlib import Path
import subprocess
import threading
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.export_media import export_project, audio_mode
from app.media import ffmpeg, probe
from app.pipeline import Pipeline
from app.server import create_app
from app.store import Store
from app.subtitles import render_vtt


def test_vtt_keeps_punctuation_and_escapes_caption_markup():
    vtt = render_vtt([dict(start=1.25, end=3.5, zh="中文, <代码>")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.250 --> 00:00:03.500" in vtt
    assert "中文, &lt;代码&gt;" in vtt


def test_audio_options_invalidate_preview_but_preserve_dry_voice(tmp_path):
    app = create_app(tmp_path / "projects")
    with TestClient(app) as client:
        store = app.state.store; p = store.create("test", "en", "course"); folder = store.folder(p["id"])
        name = "preview-" + "a" * 24 + ".mp4"
        store.update(p["id"], background_mode="none", outputs={"audio": "chinese.wav", "video": "result.mkv", "preview": name}, dub_signature="voice")
        (folder / "result.mkv").write_bytes(b"prior video")
        (folder / name).write_bytes(b"p" * 2048)
        (folder / "chinese.vtt").write_text("WEBVTT\n", encoding="utf-8")
        response = client.get(f"/api/projects/{p['id']}/files/{name}", headers={"Range": "bytes=0-9"})
        assert response.status_code == 206 and response.headers["content-type"] == "video/mp4"
        assert client.get(f"/api/projects/{p['id']}/files/chinese.vtt").headers["content-type"].startswith("text/vtt")
        result = client.put(f"/api/projects/{p['id']}/audio-options", json={"mode": "original", "gain": 1})
        assert result.status_code == 200
        data = result.json(); assert data["outputs"] == {"audio": "chinese.wav"} and data["dub_signature"] == "voice"
        assert client.get(f"/api/projects/{p['id']}/files/{name}").status_code == 404
        backup = list((folder / "history").glob("*/result.mkv")); assert len(backup) == 1 and backup[0].read_bytes() == b"prior video"
        assert client.put(f"/api/projects/{p['id']}/audio-options", json={"mode": "custom", "gain": 1}).status_code == 400
        assert client.put(f"/api/projects/{p['id']}/audio-options", json={"mode": "other", "gain": 1}).status_code == 400
        assert client.put(f"/api/projects/{p['id']}/audio-options", json={"mode": "none", "gain": 3}).status_code == 422


def write_stereo(path, data, rate=44100):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(data.shape[1]); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes((data * 32767).astype("<i2").tobytes())


def test_real_background_mix_preserves_stereo_and_preview_has_chinese_audio(tmp_path, monkeypatch):
    store = Store(tmp_path / "projects"); p = store.create("mix", "en", "course"); folder = store.folder(p["id"]);(folder / "cache").mkdir()
    settings = Settings(preview_encoder="cpu"); cancel = threading.Event(); log = folder / "process.log"
    ffmpeg(settings, ["-f", "lavfi", "-i", "color=c=green:s=320x180:r=24:d=4", "-f", "lavfi", "-i", "sine=frequency=1000:duration=4", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-f", "matroska", str(folder / "source.media")], log, cancel)
    t = np.arange(4 * 44100) / 44100
    bed = np.column_stack((.08 * np.sin(2 * np.pi * 300 * t), .08 * np.sin(2 * np.pi * 700 * t)))
    voice = (.1 * np.sin(2 * np.pi * 1200 * t))[:, None]
    write_stereo(folder / "background.media", bed); write_stereo(folder / "chinese.wav", voice)
    rows = [dict(id=1, start=0, end=4, text="Source", zh="中文", speaker="speaker_1")]
    store.update(p["id"], media={"duration": 4}, segments=rows, outputs={"audio": "chinese.wav"}, dub_signature="test", background_mode="custom", background_gain=1, background="background.media")
    pipeline = Pipeline(store)
    try: pipeline.write_subtitles(p["id"])
    finally: pipeline.close()
    export_project(store, p["id"], settings, cancel, lambda *_: None, {})
    result = store.read(p["id"]); assert result["outputs"].get("preview") and result["outputs"].get("mixed_audio")
    info = probe(folder / result["outputs"]["preview"], settings)
    assert [x["codec_name"] for x in info["streams"]] == ["h264", "aac"]
    assert len([x for x in probe(folder / "result.mkv", settings)["streams"] if x["codec_type"] == "audio"]) == 2
    data = subprocess.run([settings.ffmpeg, "-v", "error", "-i", str(folder / result["outputs"]["mixed_audio"]), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    mixed = np.frombuffer(data, dtype="<f4").reshape(-1,2)[4410:-4410]
    time_axis = np.arange(len(mixed)) / 44100
    def amplitude(signal, frequency): return 2 * abs(np.sum(signal * np.exp(-2j*np.pi*frequency*time_axis))) / len(signal)
    assert amplitude(mixed[:,0],300) > .07 and amplitude(mixed[:,1],700) > .07
    assert amplitude(mixed[:,0],700) < .005 and amplitude(mixed[:,1],300) < .005
    assert amplitude(mixed[:,0],1200) > .06
    old_hash = hashlib.sha256((folder / "result.mkv").read_bytes()).hexdigest();old_outputs=result["outputs"]
    monkeypatch.setattr("app.export_media.make_preview", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("preview failed")))
    store.update(p["id"], background_gain=.5)
    with pytest.raises(RuntimeError, match="preview failed"):
        export_project(store, p["id"], settings, cancel, lambda *_: None, {})
    assert hashlib.sha256((folder / "result.mkv").read_bytes()).hexdigest() == old_hash
    assert store.read(p["id"])["outputs"] == old_outputs


def test_legacy_background_mode_is_explicit():
    assert audio_mode({"background": ""}) == "none"
    assert audio_mode({"background": "background.media"}) == "custom"
    assert audio_mode({"background_mode": "original", "background": ""}) == "original"


def test_automatic_reference_subtracts_background_instead_of_adding_it(tmp_path, monkeypatch):
    store = Store(tmp_path / "projects"); p = store.create("reference", "en", "course"); folder = store.folder(p["id"]); (folder / "cache").mkdir()
    rate = 44100; t = np.arange(4 * rate) / rate
    voice = .1 * np.sin(2 * np.pi * 1200 * t)
    bed = np.column_stack((.08 * np.sin(2 * np.pi * 300 * t), .08 * np.sin(2 * np.pi * 700 * t)))
    write_stereo(folder / "source.media", bed + voice[:,None]); write_stereo(folder / "bed.wav", bed)
    rows = [dict(id=1, start=0, end=4, text="sample", zh="sample", speaker="speaker_1")]
    store.update(p["id"], media={"duration": 4}, segments=rows)
    monkeypatch.setattr("app.pipeline.original_background", lambda *_: (folder / "bed.wav", "known"))
    pipeline = Pipeline(store)
    try: refs = pipeline.references(p["id"], Settings(), threading.Event())
    finally: pipeline.close()
    data = subprocess.run([Settings().ffmpeg, "-v", "error", "-i", str(folder / refs["speaker_1"]), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    extracted = np.frombuffer(data, dtype="<f4")[2400:-2400]; t = np.arange(len(extracted)) / 24000
    def amplitude(frequency): return 2 * abs(np.sum(extracted * np.exp(-2j*np.pi*frequency*t))) / len(extracted)
    assert amplitude(1200) > .099
    assert amplitude(300) < .0001 and amplitude(700) < .0001
