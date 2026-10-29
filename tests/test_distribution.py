import json
from pathlib import Path

import pytest

from app.config import Settings
from scripts.check_repository import audit_files
from scripts import prepare_background


def test_publication_audit_rejects_private_artifacts_and_credentials(tmp_path):
    paths = ["projects/private/project.json", "settings.local.json", "models/weights.bin", ".env"]
    assert len(audit_files(tmp_path, paths)) == len(paths)
    safe = tmp_path / "README.md"
    safe.write_text("No private material.\n", encoding="utf-8")
    assert audit_files(tmp_path, [safe.name]) == []
    safe.write_text("ghp_" + "a" * 36, encoding="utf-8")
    errors = audit_files(tmp_path, [safe.name])
    assert len(errors) == 1 and "credential" in errors[0] and "a" * 36 not in errors[0]


def test_public_sample_uses_portable_default_settings():
    root = Path(__file__).resolve().parents[1]
    sample = json.loads((root / "settings.example.json").read_text(encoding="utf-8"))
    assert Settings.model_validate(sample) == Settings()


def test_background_environment_uses_installed_tts_python(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare_background, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_background.shutil, "which", lambda _: "uv")
    tts_python = tmp_path / "vendor/index-tts/.venv/Scripts/python.exe"
    tts_python.parent.mkdir(parents=True)
    tts_python.touch()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "venv":
            (Path(command[2]) / "Lib/site-packages").mkdir(parents=True)

    monkeypatch.setattr(prepare_background.subprocess, "run", run)
    monkeypatch.setattr(prepare_background, "download", lambda *args: None)
    prepare_background.main()
    assert commands[0][-2:] == ["--python", str(tts_python)]
    pth = tmp_path / "runtime/separation-env/Lib/site-packages/btranslate-torch.pth"
    assert pth.read_text(encoding="utf-8").strip() == str(tmp_path / "vendor/index-tts/.venv/Lib/site-packages")
    assert not pth.read_bytes().startswith(b"\xef\xbb\xbf")


def test_background_environment_reports_missing_prerequisites(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare_background, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_background.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="uv not found"):
        prepare_background.main()
    monkeypatch.setattr(prepare_background.shutil, "which", lambda _: "uv")
    with pytest.raises(RuntimeError, match="IndexTTS environment is missing"):
        prepare_background.main()
