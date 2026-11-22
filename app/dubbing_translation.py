"""Propose a short English or Japanese script from a measured alignment conflict."""
from __future__ import annotations

import asyncio
import math
import re

from .config import resolve
from .languages import TARGET_LANGUAGES, target_code
from .media import check_cancel
from .translation import _post_cancellable, local_translator
from .translation_checks import validate_values
from .translation_quality import _request, speech_seconds

HOMURA_MODEL = 'models/Index-Homura-9B.Q4_K_M.gguf'
SENTENCE_END = re.compile(r'[!?。！？]+|(?<!\d)\.(?=\s|$)')


def measured_conflict(project, row):
    report = project.get('alignment_report') or {}
    if report.get('status') != 'blocked' or report.get('revision') != project.get('edit_revision',0):
        return None
    return next((item for item in report.get('conflicts', []) if item['id'] == row['id']
                 and item['voice_seconds'] > item['window_seconds'] > 0), None)


def shorten_to_alignment(project, row, context, settings, log, cancel):
    language = target_code(project)
    conflict = measured_conflict(project,row)
    if language not in {'en','ja'} or not conflict or not row['zh'].strip():
        raise ValueError('请先生成英日语配音，选择存在实测时长冲突的句子')
    if not resolve(HOMURA_MODEL).is_file():
        raise ValueError('请先在 models 目录准备 Index-Homura-9B.Q4_K_M.gguf')
    before = speech_seconds(row['zh'],language)
    estimated_units = before * (4.2 if language == 'en' else 5.5)
    budget = max(2, math.floor(estimated_units * conflict['window_seconds']/conflict['voice_seconds']))
    target = TARGET_LANGUAGES[language]
    prompt = (f'请将以下文本翻译为{target}，译文控制在约 {budget} 个音节。'
              '保留事实、问句、数字、条件、否定和术语。保持句子数量，不增加额外请求、结论或解释。'
              '只输出译文。\n\n' + row['text'])
    if settings.glossary.strip():
        prompt += '\n\n术语参考（仅在原文出现时使用）：\n' + settings.glossary
    homura = settings.model_copy(update={'translation_provider':'llama_cpp','translation_url':'',
                                          'translation_model':HOMURA_MODEL,'translation_name':'index-homura'})
    with local_translator(homura,log,cancel) as url:
        payload = dict(model=homura.translation_model_id,messages=[dict(role='user',content=prompt)],
                       temperature=.3,max_tokens=256,stream=False,chat_template_kwargs={'enable_thinking':False})
        response=asyncio.run(_post_cancellable(url+'/chat/completions',payload,cancel)); response.raise_for_status()
        choice=response.json()['choices'][0]
        if choice.get('finish_reason')=='length':
            raise ValueError('短配音稿生成未完成，请重试')
        candidate=choice['message']['content'].rsplit('</think>',1)[-1].strip()
    if not candidate or speech_seconds(candidate,language)>=before:
        raise ValueError('Homura 没有生成更短的候选，保留当前译文')
    validate_values([row],{row['id']:{'zh':candidate}},language)
    topic=dict(title=project['name'],language=project['language'],target_language=language,mode=project['mode'])
    with local_translator(settings,log,cancel) as url:
        checked=_request(url,settings,[row],context,project['mode'],cancel,'review',{row['id']:candidate},topic)[row['id']]
    check_cancel(cancel)
    if len(SENTENCE_END.findall(checked['zh'])) > max(1,len(SENTENCE_END.findall(row['text']))):
        raise ValueError('短稿增加了原文未包含的独立句，请用原稿或重新生成')
    if speech_seconds(checked['zh'],language)>=before:
        raise ValueError('保留原意后候选没有变短，请保留当前译文并调整时间窗口')
    reason=f"Homura 按实测配音 {conflict['voice_seconds']:.2f} 秒、字幕窗口 {conflict['window_seconds']:.2f} 秒生成短稿，目标 {budget} 音节。请试听确认实际时长。"
    return dict(after_source=row['text'],after=checked['zh'],reason=reason,checks=checked.get('checks',[]))
