"""Project goals, explicit ASR choices and reviewable model provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .config import Settings, load_settings, resolve
from .languages import SOURCE_LANGUAGES

GOALS = {'transcribe', 'translate', 'dub'}
ASR_FIELDS = {'asr_provider', 'asr_device', 'fun_asr_hotwords'}


def goal(project):
    return project.get('goal', 'dub')


def steps(project):
    return ['analyze'] + ([] if goal(project) == 'transcribe' else ['translate']) + (['dub', 'export'] if goal(project) == 'dub' else [])


def summary(project):
    rows = project.get('segments', [])
    translated = sum(bool(r.get('zh', '').strip()) for r in rows)
    outputs = project.get('outputs', {})
    code = ('video_ready' if outputs.get('video') or outputs.get('preview') else
            'audio_ready' if outputs.get('audio') else
            'translation_ready' if rows and translated == len(rows) else
            'source_ready' if rows else 'needs_source')
    return dict(goal=goal(project), code=code, source_count=len(rows), translation_count=translated)


def effective_asr(settings, project):
    overrides = {key: value for key, value in project.get('asr_options', {}).items() if key in ASR_FIELDS}
    return Settings.model_validate({**settings.model_dump(), **overrides,
                                    'translation_api_key': settings.translation_api_key})


def validate_language(settings, language):
    if language not in SOURCE_LANGUAGES:
        raise ValueError('无效的原声语言')
    if settings.asr_provider == 'fun_asr':
        from .fun_asr import language_name
        language_name(language)


def asr_profile(settings, language=''):
    provider = settings.asr_provider
    labels = dict(faster_whisper='faster-whisper', fun_asr='Fun-ASR-Nano', openai='本地兼容识别接口')
    model = settings.asr_name if provider == 'openai' else str(resolve(settings.fun_asr_model if provider == 'fun_asr' else settings.asr_model))
    value = dict(provider=provider, label=labels[provider], model=model, language=language,
                 device='service' if provider == 'openai' else settings.asr_device,
                 hotwords=settings.fun_asr_hotwords if provider == 'fun_asr' else '',
                 supported_languages=['', 'zh', 'en', 'ja'] if provider == 'fun_asr' else list(SOURCE_LANGUAGES))
    checkpoint=Path(model)/('model.pt' if provider=='fun_asr' else 'model.bin')
    stamp=checkpoint.stat().st_mtime_ns if provider!='openai' and checkpoint.is_file() else 0
    value['signature'] = hashlib.sha256(json.dumps([value, settings.asr_revision,stamp], sort_keys=True).encode()).hexdigest()[:24]
    return value


def last_asr(store, identifier, project):
    if project.get('asr_last'):
        return project['asr_last']
    path = store.folder(identifier) / 'cache/asr-words-metadata.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return dict(provider=value.get('provider', 'faster_whisper'), model=value.get('model', ''),
                    language=value.get('language', ''), historical=True)
    except (OSError, ValueError, TypeError):
        return None


def enrich(store, project, settings):
    settings = effective_asr(settings, project)
    return dict(workflow=summary(project), asr_next=asr_profile(settings, project['language']),
                asr_last=last_asr(store, project['id'], project))


class GoalChange(BaseModel):
    revision: int = Field(ge=0)
    goal: Literal['transcribe', 'translate', 'dub']


class ASRChange(BaseModel):
    revision: int = Field(ge=0)
    provider: Literal['faster_whisper', 'fun_asr', 'openai']
    language: str
    device: Literal['cuda', 'cpu'] = 'cuda'
    hotwords: str = Field(default='', max_length=2000)


class SourceCopy(BaseModel):
    revision: int = Field(ge=0)


def attach(app, store, pipeline, editable):
    def revision(identifier, value):
        editable(identifier)
        project = store.read(identifier)
        if value != project.get('edit_revision', 0):
            raise HTTPException(409, '项目已经更新，请保留草稿并刷新后重试')
        return project

    @app.get('/api/asr-profile')
    def default_profile():
        settings = load_settings()
        return asr_profile(settings, 'zh' if settings.asr_provider == 'fun_asr' else '')

    @app.put('/api/projects/{identifier}/goal')
    def change_goal(identifier: str, data: GoalChange):
        with pipeline.lock:
            project = revision(identifier, data.revision)
            result = store.update(identifier, goal=data.goal, edit_revision=project.get('edit_revision', 0)+1)
            return {**result, **enrich(store, result, load_settings())}

    @app.put('/api/projects/{identifier}/asr-options')
    def change_asr(identifier: str, data: ASRChange):
        with pipeline.lock:
            project = revision(identifier, data.revision)
            options = dict(asr_provider=data.provider, asr_device=data.device, fun_asr_hotwords=data.hotwords)
            settings = effective_asr(load_settings(), {**project, 'asr_options': options})
            validate_language(settings, data.language)
            result = store.update(identifier, asr_options=options, language=data.language, edit_revision=project.get('edit_revision', 0)+1)
            return {**result, **enrich(store, result, settings)}

    @app.post('/api/projects/{identifier}/copy-source')
    def copy_source(identifier: str, data: SourceCopy):
        with pipeline.lock:
            project = revision(identifier, data.revision)
            if project['language'] != project.get('target_language', 'zh'):
                raise ValueError('仅原语言与目标语言相同时可使用原文作为配音稿')
            if not project['segments']:
                raise ValueError('请先识别或导入原文字幕')
            pipeline.snapshot(identifier)
            rows = [{**row, 'zh': row['text']} for row in project['segments']]
            result = store.update(identifier, segments=rows, outputs={}, dub_signature='', export_signature='',
                                  translation_checks={}, audition=None, status='idle', error='')
            pipeline.write_subtitles(identifier)
            return {**result, **enrich(store, result, load_settings())}

    @app.post('/api/projects/{identifier}/asr-runtime')
    def check_runtime(identifier: str):
        project=store.read(identifier)
        settings = effective_asr(load_settings(), project)
        profile = asr_profile(settings,project['language'])
        if settings.asr_provider == 'openai':
            return dict(ok=False, level='service', message='服务运行环境由外部服务管理，请使用连接检查或短音频试识别')
        from .fun_asr import python_path
        python = python_path(settings) if settings.asr_provider == 'fun_asr' else Path(sys.executable)
        code = ('import importlib.metadata as m; '
                '[m.version(p) for p in ["torch","torchaudio","funasr","transformers","tiktoken","soundfile"]]; '
                'import torch; print("cuda="+str(torch.cuda.is_available()))') if settings.asr_provider == 'fun_asr' else 'from faster_whisper import WhisperModel; import ctranslate2; print("cuda="+str(ctranslate2.get_cuda_device_count()>0))'
        try:
            result = subprocess.run([str(python), '-c', code], capture_output=True, timeout=40,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            cuda_ok = settings.asr_device != 'cuda' or b'cuda=True' in result.stdout
            ok = result.returncode == 0 and cuda_ok
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        return dict(ok=ok, level='environment', signature=profile['signature'],
                    message='运行环境可用；尚未加载模型或验证推理' if ok else '运行环境检查失败，请检查独立 Python、依赖和 CUDA')
