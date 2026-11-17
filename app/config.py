from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from .file_io import atomic_json

from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "projects"
SETTINGS = ROOT / "settings.local.json"


class Settings(BaseModel):
    asr_provider: Literal['faster_whisper', 'fun_asr', 'openai'] = 'faster_whisper'
    fun_asr_model: str = 'models/Fun-ASR-Nano-2512'
    fun_asr_vad_model: str = 'models/fsmn-vad'
    fun_asr_python: str = ''
    fun_asr_hotwords: str = Field(default='', max_length=2000)
    asr_url: str = ''
    asr_name: str = 'whisper-1'
    asr_revision: str = ''
    translation_revision: str = ''
    translation_provider: Literal['auto', 'llama_cpp', 'openai'] = 'auto'
    translation_json_mode: bool = True
    tts_provider: Literal['indextts', 'openai_speech', 'btv_reference'] = 'indextts'
    tts_url: str = ''
    tts_name: str = 'local-tts'
    tts_voice: str = 'default'
    tts_languages: str = 'zh,en,ja,es,ar'
    tts_revision: str = ''
    model_service_timeout: int = Field(default=180, ge=5, le=3600)
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
    alignment_mode: Literal['adaptive','strict'] = 'adaptive'
    alignment_fallback_speed: float = Field(default=1.5, ge=1, le=2)
    alignment_fallback_lead: float = Field(default=1.5, ge=0, le=1.5)
    alignment_fallback_lag: float = Field(default=3, ge=0, le=3)
    audio_workers: int = Field(default=4, ge=1, le=8)
    tts_batch_size: int = Field(default=4, ge=1, le=4)
    tts_idle_minutes: float = Field(default=10, ge=0, le=1440)
    tts_keep_loaded: bool = False
    diarization_model: str = "models/speaker-diarization-community-1"
    diarization_python: str = ""
    diarization_device: Literal["auto", "cuda", "cpu"] = "auto"
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

    @field_validator("translation_url", "asr_url", "tts_url")
    @classmethod
    def local_only(cls, value):
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("完全本地模式仅接受本机 HTTP 地址")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("本机服务端口需要在 1～65535 之间")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("本机服务地址不能包含认证信息、查询参数或片段")
        return value.rstrip("/")

    @field_validator('asr_name', 'translation_name', 'tts_name', 'tts_voice', 'tts_revision', 'asr_revision', 'translation_revision')
    @classmethod
    def bounded_model_text(cls, value):
        value = value.strip()
        if len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError('模型名称、音色和修订标识最多 200 字，不能包含控制字符')
        return value

    @field_validator('tts_languages')
    @classmethod
    def supported_languages(cls, value):
        codes = list(dict.fromkeys(c.strip() for c in value.split(',') if c.strip()))
        if not codes or set(codes) - {'zh', 'en', 'ja', 'es', 'ar'}:
            raise ValueError('配音语言请填写 zh,en,ja,es,ar 中的语言代码，以逗号分隔')
        return ','.join(codes)

    @model_validator(mode='after')
    def service_configuration(self):
        if self.asr_provider == 'openai' and (not self.asr_url or not self.asr_name):
            raise ValueError('本地识别接口需要服务地址和模型名称')
        if self.translation_provider == 'openai' and (not self.translation_url or not self.translation_name):
            raise ValueError('本地翻译接口需要服务地址和模型名称')
        if self.tts_provider != 'indextts' and (not self.tts_url or not self.tts_name):
            raise ValueError('本地配音接口需要服务地址和模型名称')
        if self.tts_provider == 'openai_speech' and not self.tts_voice:
            raise ValueError('普通语音接口需要服务音色 ID')
        return self

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
    result = {
        "root": str(ROOT),
        "ffmpeg": tool(settings.ffmpeg),
        "ffprobe": tool(settings.ffprobe),
        "llama": str(llama) if llama else None,
        "local_only": True,
        "separation_ready": separation_python().is_file() and all((resolve(settings.separation_model) / name).is_file() for name in ["config.json", "htdemucs.safetensors", "manifest.json"]),
    }

    from .model_backends import model_readiness
    result.update(model_readiness(settings))
    return result
