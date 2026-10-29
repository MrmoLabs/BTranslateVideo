from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "projects"
SETTINGS = ROOT / "settings.local.json"


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Settings(BaseModel):
    asr_model: str = "models/faster-whisper-large-v3"
    asr_device: str = "cuda"
    asr_compute_type: str = "float16"
    translation_model: str = "models/Qwen3-8B-Q4_K_M.gguf"
    translation_url: str = ""
    translation_name: str = "local-translator"
    llama_executable: str = ""
    index_root: str = "vendor/index-tts"
    index_model: str = "models/IndexTTS-2.5"
    index_version: str = "2.5"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_speed: float = Field(default=1.35, ge=1.0, le=2.0)
    max_lead: float = Field(default=.75, ge=0, le=1.5)
    max_lag: float = Field(default=1.0, ge=0, le=3.0)
    audio_workers: int = Field(default=4, ge=1, le=8)
    tts_batch_size: int = Field(default=4, ge=1, le=4)
    tts_idle_minutes: int = Field(default=10, ge=0, le=60)
    separation_model: str = "models/demucs"
    preview_encoder: str = "auto"

    translation_batch: int = Field(default=6, ge=1, le=12)
    translation_review: str = "auto"
    translation_compact: bool = True
    glossary: str = Field(default="", max_length=20000)

    @field_validator("translation_review")
    @classmethod
    def review_choice(cls, value):
        if value not in {"auto", "always", "off"}: raise ValueError("无效语义审校模式")
        return value

    @field_validator("preview_encoder")
    @classmethod
    def preview_choice(cls, value):
        if value not in {"auto", "cpu"}: raise ValueError("无效预览编码方式")
        return value

    @field_validator("translation_url")
    @classmethod
    def local_only(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("完全本地模式仅接受本机 HTTP 地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("本机服务地址不能包含认证信息、查询参数或片段")
        return value.rstrip("/")

    @field_validator("asr_device")
    @classmethod
    def device_choice(cls, value):
        if value not in {"cuda", "cpu"}:
            raise ValueError("ASR device must be cuda or cpu")
        return value

    @field_validator("index_version")
    @classmethod
    def version_choice(cls, value):
        if value not in {"2", "2.5"}:
            raise ValueError("IndexTTS version must be 2 or 2.5")
        return value


def resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_settings() -> Settings:
    if SETTINGS.exists():
        return Settings.model_validate_json(SETTINGS.read_text(encoding="utf-8-sig"))
    return Settings()


def executable(value: str) -> str:
    path = resolve(value)
    found = str(path) if path.is_file() else shutil.which(value)
    if not found:
        raise RuntimeError(f"找不到程序：{value}")
    return found


def llama_path(settings: Settings) -> Path | None:
    if settings.llama_executable:
        return resolve(settings.llama_executable)
    return next((ROOT / "runtime" / "llama").rglob("llama-server.exe"), None)


def index_python(settings: Settings) -> Path:
    return resolve(settings.index_root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def separation_python():
    return ROOT / "runtime" / "separation-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def diagnostics(settings: Settings) -> dict:
    def tool(name):
        try:
            return executable(name)
        except RuntimeError:
            return None
    llama = llama_path(settings)
    return {
        "root": str(ROOT),
        "ffmpeg": tool(settings.ffmpeg),
        "ffprobe": tool(settings.ffprobe),
        "asr_ready": (resolve(settings.asr_model) / "model.bin").is_file(),
        "translation_ready": bool(settings.translation_url) or (bool(llama and llama.is_file()) and resolve(settings.translation_model).is_file()),
        "tts_ready": index_python(settings).is_file() and all((resolve(settings.index_model) / name).is_file() for name in ["config.yaml", "gpt.pth", "codec.pth", "s2mel.pth", "hf_cache/campplus_cn_common.bin", "hf_cache/w2v-bert-2.0/config.json", "hf_cache/w2v-bert-2.0/model.safetensors", "hf_cache/bigvgan/config.json", "hf_cache/bigvgan/bigvgan_generator.pt"]),
        "llama": str(llama) if llama else None,
        "local_only": True,
        "separation_ready": separation_python().is_file() and all((resolve(settings.separation_model) / name).is_file() for name in ["config.json", "htdemucs.safetensors", "manifest.json"]),
    }
