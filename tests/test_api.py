from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.server import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "projects")
    with TestClient(app) as client:
        yield client


def create_project(client):
    store = client.app.state.store
    p = store.create("test", "en", "course")
    store.update(p["id"], media={"duration": 20}, segments=[{"id": 1, "start": 0, "end": 3, "text": "Hello", "zh": "你好", "speaker": "speaker_1"}], outputs={"audio": "chinese.wav"}, dub_signature="old")
    return p["id"]


def test_editor_invalidates_changed_source_translation_and_audio(client):
    identifier = create_project(client)
    p = client.get(f"/api/projects/{identifier}").json()
    p["segments"][0]["text"] = "Goodbye"
    response = client.put(f"/api/projects/{identifier}/segments", json={"segments": p["segments"]})
    assert response.status_code == 200
    assert response.json()["segments"][0]["zh"] == ""
    assert response.json()["outputs"] == {}
    assert response.json()["dub_signature"] == ""


def test_busy_project_rejects_edits(client):
    identifier = create_project(client)
    import threading
    client.app.state.pipeline.events[identifier] = threading.Event()
    try:
        segments = client.get(f"/api/projects/{identifier}").json()["segments"]
        assert client.put(f"/api/projects/{identifier}/segments", json={"segments": segments}).status_code == 409
    finally:
        client.app.state.pipeline.events.pop(identifier)


def test_srt_import_checks_alignment(client):
    identifier = create_project(client)
    url = f"/api/projects/{identifier}/subtitles"
    wrong = "1\n00:00:01,000 --> 00:00:04,000\n新译文\n"
    assert client.post(url, files={"subtitle": ("zh.srt", wrong.encode())}, data={"kind": "zh"}).status_code == 400
    right = "1\n00:00:00,000 --> 00:00:03,000\n新译文\n"
    response = client.post(url, files={"subtitle": ("zh.srt", right.encode())}, data={"kind": "zh"})
    assert response.status_code == 200
    assert response.json()["segments"][0]["zh"] == "新译文"


def test_files_and_cross_site_operations_are_restricted(client):
    identifier = create_project(client)
    assert client.get(f"/api/projects/{identifier}/files/project.json").status_code == 404
    assert client.post(f"/api/projects/{identifier}/cancel", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(f"/api/projects/{identifier}/cancel", headers={"Host": "evil.example"}).status_code == 403


def test_ui_and_invalid_stage(client):
    assert "BTranslateVideo" in client.get("/").text
    identifier = create_project(client)
    assert client.post(f"/api/projects/{identifier}/run/unknown").status_code == 400


def test_stale_editor_cannot_overwrite_new_translation(client):
    identifier = create_project(client)
    original = client.get(f"/api/projects/{identifier}").json()
    store = client.app.state.store
    changed = [dict(item) for item in original["segments"]]
    changed[0]["zh"] = "刚更新的译文"
    store.update(identifier, segments=changed)
    response = client.put(f"/api/projects/{identifier}/segments", json={"segments": original["segments"], "revision": original["edit_revision"]})
    assert response.status_code == 409
    assert store.read(identifier)["segments"][0]["zh"] == "刚更新的译文"


def test_shutdown_uses_server_callback_and_rejects_cross_site(client):
    calls = []
    client.app.state.request_shutdown = lambda: calls.append(True)
    assert client.post("/api/shutdown", headers={"Origin": "https://evil.example"}).status_code == 403
    assert calls == []
    assert client.post("/api/shutdown").status_code == 200
    assert calls == [True]
