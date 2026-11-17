"""Lightweight local Fun-ASR paths and timestamp validation (no ML imports)."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_FILES = ('config.yaml', 'configuration.json', 'model.pt', 'multilingual.tiktoken',
               'Qwen3-0.6B/config.json', 'Qwen3-0.6B/tokenizer_config.json',
               'Qwen3-0.6B/tokenizer.json')
VAD_FILES = ('config.yaml', 'model.pt', 'am.mvn')


def local_path(value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def python_path(settings):
    return local_path(settings.fun_asr_python) if settings.fun_asr_python else ROOT / 'runtime/fun-asr-env' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def ready(settings):
    return python_path(settings).is_file() and all((local_path(settings.fun_asr_model) / name).is_file() for name in MODEL_FILES) and all((local_path(settings.fun_asr_vad_model) / name).is_file() for name in VAD_FILES)


def worker_request(settings):
    if not ready(settings):
        raise ValueError('Fun-ASR 环境或模型未准备，请先运行 .venv/Scripts/python.exe scripts/prepare_fun_asr.py')
    return dict(model=str(local_path(settings.fun_asr_model)), vad_model=str(local_path(settings.fun_asr_vad_model)),
                device=settings.asr_device, hotwords=[w.strip() for w in re.split(r'[,，\n]', settings.fun_asr_hotwords) if w.strip()])


def cache_signature(settings):
    files = []
    for folder, names in [(local_path(settings.fun_asr_model), MODEL_FILES), (local_path(settings.fun_asr_vad_model), VAD_FILES)]:
        # Include tokenizer and config replacements as well as the large checkpoint.
        for name in names:
            path = folder / name
            stat = path.stat() if path.is_file() else None
            files.append([str(path), stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0])
    return dict(provider='fun_asr', adapter_version=1, files=files, hotwords=settings.fun_asr_hotwords, revision=settings.asr_revision)


def language_name(language):
    if not language or language == 'auto':
        return None
    names = {'zh': '中文', 'en': '英文', 'ja': '日文'}
    if language not in names:
        raise ValueError('Fun-ASR-Nano 支持中文（含川渝口音）、英文和日文；请更改原声语言或选择其他识别后端')
    return names[language]


def lexical(text):
    return ''.join(re.findall(r'\w+', text, flags=re.UNICODE)).casefold()


def timed_segment(result, offset, duration, identifier):
    """Native Fun-ASR timestamps are token/start_time/end_time in seconds.

    Never substitute ctc_text, VAD boundaries, or evenly distributed timing for
    the actual decoded text. Incomplete/old checkpoints must fail visibly.
    """
    if not isinstance(result, dict) or not isinstance(result.get('text'), str):
        raise ValueError('Fun-ASR 返回了无效识别结果')
    text = result['text'].strip()
    if not text:
        return None
    stamps = result.get('timestamps')
    if not isinstance(stamps, list) or not stamps:
        raise ValueError('Fun-ASR 模型未返回真实字级时间戳，请通过 prepare_fun_asr.py 准备 ModelScope Nano 完整 CTC 权重；纯文本模型不能用于字幕对齐')
    words = []
    for item in stamps:
        if not isinstance(item, dict) or not isinstance(item.get('token'), str):
            raise ValueError('Fun-ASR 字级时间戳格式无效')
        token = item['token']
        if not token.strip():
            continue
        try:
            start, end = float(item['start_time']), float(item['end_time'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Fun-ASR 缺少有效字级时间戳') from None
        if not math.isfinite(start + end) or start < 0 or end < start or end > duration + .1 or words and start < words[-1]['start'] - offset - .02:
            raise ValueError('Fun-ASR 字级时间戳越界或顺序错误')
        words.append(dict(word=token, start=round(offset + start, 3), end=round(offset + min(end, duration), 3)))
    if not words or lexical(''.join(w['word'] for w in words)) != lexical(text):
        raise ValueError('Fun-ASR 字级时间戳未覆盖全部原文，请检查模型和运行环境')
    # CTC emits punctuation as separate tokens. The subtitle boundary protocol
    # counts spoken words; attach punctuation without assigning it extra speech
    # time or dropping any lexical character. Keep native speech timings intact.
    normalized, leading = [], ''
    for word in words:
        if lexical(word['word']):
            normalized.append({**word, 'word': leading + word['word']})
            leading = ''
        elif normalized:
            normalized[-1]['word'] += word['word']
        else:
            leading += word['word']
    if not normalized:
        return None
    words = normalized
    return dict(id=identifier, start=words[0]['start'], end=max(w['end'] for w in words), text=text,
                zh='', speaker='speaker_1', words=words)
