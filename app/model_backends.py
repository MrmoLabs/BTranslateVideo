"""Adapter boundary for native models, local services and the Bilibili translation API.

Only the native adapters own model processes. HTTP adapters own requests, not servers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import ROOT, Settings, atomic_json, index_python, llama_path, resolve
from .emotions import emotion_fields
from .languages import target_code, tts_language
from .project_roles import UNASSIGNED_SPEAKER
from .media import check_cancel, ffmpeg, run, wav_duration


@dataclass(frozen=True)
class Backend:
    id: str
    label: str
    local_process: bool = False
    reference_audio: bool = False
    emotions: bool = False
    word_timestamps: bool = False


BACKENDS = {
    'asr': [Backend('faster_whisper', 'faster-whisper', local_process=True, word_timestamps=True),
            Backend('fun_asr', 'Fun-ASR-Nano · 川渝方言', local_process=True, word_timestamps=True),
            Backend('openai', '本地兼容识别接口', word_timestamps=True)],
    'translation': [Backend('auto', '自动兼容旧配置', local_process=True),
                    Backend('llama_cpp', 'llama.cpp 本地 GGUF', local_process=True),
                    Backend('openai', '本地兼容对话接口'),
                    Backend('openai_remote', '网络翻译 API（OpenAI 兼容）'),
                    Backend('bilibili', 'B 站官方翻译 API')],
    'tts': [Backend('indextts', 'IndexTTS', local_process=True, reference_audio=True, emotions=True),
            Backend('openai_speech', '本地兼容语音接口'),
            Backend('btv_reference', '本地参考音桥接 · BTV v1', reference_audio=True, emotions=True)],
}


def catalog():
    return {stage: [asdict(backend) for backend in backends] for stage, backends in BACKENDS.items()}


def local_translation_models(directory):
    directory = directory.resolve()
    models = []
    for path in directory.rglob('*.[gG][gG][uU][fF]'):
        if not path.is_file() or not path.resolve().is_relative_to(directory):
            continue
        shard = re.search(r'-(\d{5})-of-\d{5}\.gguf$', path.name, re.IGNORECASE)
        if shard and shard[1] != '00001':
            continue
        models.append('models/' + path.relative_to(directory).as_posix())
    return sorted(models, key=str.casefold)


def uses_translation_service(settings):
    return settings.translation_provider in {'openai', 'openai_remote', 'bilibili'} or (settings.translation_provider == 'auto' and bool(settings.translation_url))


def translation_parallelism(settings):
    return 1 if uses_translation_service(settings) and settings.translation_provider != 'openai_remote' else settings.translation_parallel


def translation_output_budget(settings, default, rows):
    if settings.translation_provider != 'openai_remote' or len(rows) <= 8:
        return default
    length = sum(len(row["text"]) for row in rows)
    return min(8192, max(default, length + 64 * len(rows)))


class TranslationCapacityError(ValueError):
    """The request exceeded the translation endpoint's input or output capacity."""


def translation_input_too_large(response):
    if response.status_code == 413:
        return True
    if response.status_code not in (400, 422):
        return False
    try:
        return response.json()['error']['code'] == 'context_length_exceeded'
    except (ValueError, KeyError, TypeError):
        return False


def chat_payload(settings, payload):
    """Do not send llama.cpp-specific fields to a generic OpenAI-compatible server."""
    payload = dict(payload)
    if uses_translation_service(settings) and settings.translation_provider != 'bilibili':
        payload.pop('chat_template_kwargs', None)
    if (settings.translation_provider == 'openai_remote'
            and urlsplit(settings.translation_service_url).hostname == 'api.siliconflow.cn'
            and settings.translation_model_id in {'deepseek-ai/DeepSeek-V3.2', 'Pro/deepseek-ai/DeepSeek-V3.2'}):
        payload['enable_thinking'] = False
    if not settings.translation_json_mode:
        payload.pop('response_format', None)
    elif settings.translation_provider == 'openai_remote' and 'response_format' in payload:
        payload['response_format'] = {'type': 'json_object'}
    return payload


def tts_backend(settings):
    return next(backend for backend in BACKENDS['tts'] if backend.id == settings.tts_provider)


def validate_tts(settings, project, rows=()):
    if any(row.get('speaker')==UNASSIGNED_SPEAKER for row in rows):
        raise ValueError('有台词尚未分配角色，请在配音表格中批量指定角色后再生成配音或试听')
    code = target_code(project)
    if settings.tts_provider == 'indextts':
        tts_language(project, settings.index_version)
    elif code not in settings.tts_languages.split(','):
        raise ValueError('当前配音接口未配置这个目标语言，请检查设置中的支持语言')
    if settings.tts_provider != 'indextts' and any(row.get('emotion') == 'source' for row in rows):
        raise ValueError('跟随本句原声需要本地 IndexTTS，请切换模型或修改情绪设置')
    if not tts_backend(settings).emotions and any(emotion_fields(row) for row in rows):
        raise ValueError('普通语音接口不支持参考音或情绪；请将情绪设为沿用，或切换 IndexTTS / 参考音桥接接口')
    return code.upper()


def tts_model_signature(settings):
    if settings.tts_provider != 'indextts':
        return dict(protocol=1, provider=settings.tts_provider, url=settings.tts_url,
                    model=settings.tts_name, voice=settings.tts_voice if settings.tts_provider == 'openai_speech' else '',
                    languages=settings.tts_languages, revision=settings.tts_revision)
    model = resolve(settings.index_model)
    return sorted((str(p.relative_to(model)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in model.rglob('*') if p.is_file() and '.cache' not in p.parts)


def model_readiness(settings):
    from .fun_asr import ready as fun_asr_ready
    native = settings.tts_provider == 'indextts'
    model = resolve(settings.index_model)
    required = ['config.yaml', 'gpt.pth', 'codec.pth', 's2mel.pth', 'hf_cache/campplus_cn_common.bin',
                'hf_cache/w2v-bert-2.0/config.json', 'hf_cache/w2v-bert-2.0/model.safetensors',
                'hf_cache/bigvgan/config.json', 'hf_cache/bigvgan/bigvgan_generator.pt']
    llama = llama_path(settings)
    return dict(asr_ready=bool(settings.asr_url) if settings.asr_provider == 'openai' else fun_asr_ready(settings) if settings.asr_provider == 'fun_asr' else (resolve(settings.asr_model)/'model.bin').is_file(),
        translation_ready=bool(settings.translation_service_url) if uses_translation_service(settings) else bool(llama and llama.is_file() and resolve(settings.translation_model).is_file()),
        tts_ready=(index_python(settings).is_file() and all((model/name).is_file() for name in required)) if native else bool(settings.tts_url),
        service_configured=any([settings.asr_provider == 'openai', uses_translation_service(settings), not native]),
        backends={'asr':settings.asr_provider, 'translation':settings.translation_provider, 'tts':settings.tts_provider})


async def cancellable(awaitable, cancel):
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            check_cancel(cancel)
            await asyncio.wait({task}, timeout=.1)
        check_cancel(cancel)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def timeout(settings):
    return httpx.Timeout(settings.model_service_timeout, connect=5)


def check_response(response):
    if response.status_code >= 300:
        raise ValueError(f'本地模型接口返回 HTTP {response.status_code}；请检查模型名称、接口协议和服务日志')


def parse_transcription(value, duration):
    if not isinstance(value, dict) or not isinstance(value.get('words'), list) or not value['words']:
        raise ValueError('识别接口必须返回 verbose_json 和 words 词级时间戳，不能仅返回纯文本或句级时间戳')
    words=[]
    for item in value['words']:
        if not isinstance(item,dict) or not isinstance(item.get('word'),str) or not item['word'].strip():
            raise ValueError('识别接口返回了无效词项')
        try:
            start,end=float(item['start']),float(item['end'])
        except (KeyError,TypeError,ValueError):
            raise ValueError('识别接口缺少有效词级时间戳') from None
        if not math.isfinite(start+end) or start<0 or end<start or end>duration+.1 or words and start<words[-1]['start']-.02:
            raise ValueError('识别接口的词级时间戳越界或顺序错误')
        words.append(dict(word=item['word'],start=start,end=min(end,duration)))
    from .segmentation import lexical
    text=' '.join(w['word'] for w in words)
    if value.get('text') and lexical(text)!=lexical(value['text']):
        raise ValueError('识别接口的词级时间戳未覆盖全部原文，请使用支持完整词级对齐的服务')
    return [dict(id=1,start=words[0]['start'],end=words[-1]['end'],text=text,zh='',speaker='speaker_1',words=words)]


def transcribe(settings,audio,output,language,mode,log,cancel,env,on_progress=None):
    if settings.asr_provider in {'faster_whisper', 'fun_asr'}:
        progress_path=Path(output).parent/'asr-progress.json';progress_path.unlink(missing_ok=True)
        request=dict(progress=str(progress_path),model=str(resolve(settings.asr_model)),audio=str(audio),language=language,mode=mode,
                     device=settings.asr_device,compute_type=settings.asr_compute_type,output=str(output))
        worker=ROOT/'scripts/model_worker.py';python=sys.executable;arguments=['asr']
        if settings.asr_provider == 'fun_asr':
            from .fun_asr import worker_request, python_path, language_name
            language_name(language)
            request.update(worker_request(settings));worker=ROOT/'scripts/fun_asr_worker.py';python=python_path(settings);arguments=[]
        path=Path(output).parent/'asr-request.json';atomic_json(path,request)
        previous=[None]
        def tick():
            if not on_progress or not progress_path.exists():return
            try:value=json.loads(progress_path.read_text(encoding='utf-8'))
            except (OSError,ValueError):return
            if value!=previous[0]:previous[0]=value;on_progress(value)
        run([python,worker,*arguments,path],log,cancel,env=env,on_tick=tick)
        return
    async def request():
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=timeout(settings)) as client:
            with Path(audio).open('rb') as stream:
                data={'model':settings.asr_name,'response_format':'verbose_json','timestamp_granularities[]':'word'}
                if language:data['language']=language
                response=await client.post(settings.asr_url+'/audio/transcriptions',data=data,files={'file':('audio.wav',stream,'audio/wav')})
            check_response(response)
            return response.json()
    if on_progress:on_progress(dict(phase='asr_service',message='本地识别服务处理音频，等待词级时间戳'))
    value=asyncio.run(cancellable(request(),cancel))
    result=parse_transcription(value,wav_duration(Path(audio)))
    check_cancel(cancel);atomic_json(Path(output),result)


def synthesize_service(settings,clips,total,folder,cancel,progress):
    """Serial requests preserve the shared queue; the server owns model residency/batching."""
    backend=tts_backend(settings)
    for clip in clips:
        validate_tts(settings,clip.get('lang','zh').lower(),[clip])
        if backend.reference_audio and not Path(clip['reference']).is_file():
            raise ValueError('参考音桥接接口需要有效参考音')
    progress(0,len(clips))
    for index,clip in enumerate(clips):
        check_cancel(cancel)
        output=Path(clip['output']);output.parent.mkdir(parents=True,exist_ok=True)
        download=output.with_suffix('.download');temporary=output.with_suffix('.pending.wav')
        payload=dict(model=settings.tts_name,input=clip['text'],response_format='wav')
        endpoint='/audio/speech'
        if backend.reference_audio:
            source=Path(clip['reference'])
            if source.stat().st_size>10*1024*1024:raise ValueError('桥接接口参考音最多 10 MiB')
            payload.update(protocol='btv-tts-v1',language=clip.get('lang','ZH').lower(),
                reference_audio={'mime_type':'audio/wav','data_base64':base64.b64encode(source.read_bytes()).decode('ascii')},
                emotion={'preset':clip.get('emotion','inherit'),'strength':clip.get('emotion_strength','medium')})
            endpoint='/synthesize'
        else:payload['voice']=settings.tts_voice
        async def request_audio():
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=timeout(settings)) as client:
                async with client.stream('POST',settings.tts_url+endpoint,json=payload) as response:
                    check_response(response)
                    size=0
                    with download.open('wb') as stream:
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>100*1024*1024:raise ValueError('单段配音响应超过 100 MiB')
                            stream.write(chunk)
        try:
            asyncio.run(cancellable(request_audio(),cancel))
            try:
                if wav_duration(download)<=0:raise ValueError('配音接口返回空音频')
            except (wave.Error,EOFError):
                raise ValueError('配音接口必须返回有效 WAV 音频') from None
            ffmpeg(settings,['-i',str(download),'-ar','24000','-ac','1','-c:a','pcm_s16le',str(temporary)],folder/'process.log',cancel)
            check_cancel(cancel);os.replace(temporary,output)
        finally:
            download.unlink(missing_ok=True);temporary.unlink(missing_ok=True)
        progress(index+1,len(clips))


def check_backend(stage,settings):
    if stage not in BACKENDS:raise ValueError('无效模型阶段')
    provider=getattr(settings,stage+'_provider')
    if stage == 'translation' and provider == 'openai_remote':
        from threading import Event
        from .translation import _post_cancellable, parse_translation, translation_http_options

        prompt = 'Translate “Hello, world.” into Simplified Chinese. Return a JSON object only: {"translations":[{"id":1,"translation":"..."}]}.'
        payload = chat_payload(settings, dict(model=settings.translation_model_id,
            messages=[dict(role='user', content=prompt)], stream=False, response_format={'type': 'json_object'}))
        options = {**translation_http_options(settings), 'read_timeout': min(25, settings.model_service_timeout)}
        try:
            response = asyncio.run(_post_cancellable(settings.translation_service_url + '/chat/completions',
                payload, Event(), **options))
            if not response.is_success:
                return dict(ok=False, level='connection', message=f'网络翻译 API 请求失败（HTTP {response.status_code}）')
            parse_translation(response.json()['choices'][0]['message']['content'], [1])
        except httpx.RequestError:
            return dict(ok=False, level='connection', message='无法连接网络翻译 API，请检查地址和网络')
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return dict(ok=False, level='inference', message='网络翻译 API 已响应，但测试译文未通过字幕格式检查')
        return dict(ok=True, level='inference', message='网络翻译 API 测试通过')
    if stage == 'translation' and provider == 'bilibili':
        from threading import Event
        from .translation import _post_cancellable, parse_translation
        from .translation_quality import bilibili_subtitle_prompt

        prompt = bilibili_subtitle_prompt('en', {1: '你好，世界。'})
        payload = dict(model=settings.translation_model_id, messages=[dict(role='user', content=prompt)],
                       temperature=0, max_tokens=128, stream=False, chat_template_kwargs={'enable_thinking': False})
        try:
            response = asyncio.run(_post_cancellable(settings.translation_service_url + '/chat/completions', payload, Event(), read_timeout=25))
            parse_translation(response.json()['choices'][0]['message']['content'], [1], allow_id_map=True)
        except RuntimeError as error:
            return dict(ok=False, level='connection', message=str(error))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return dict(ok=False, level='inference', message='B 站 API 已响应，但测试译文未通过字幕格式检查')
        return dict(ok=True, level='inference', message='B 站 API 测试翻译通过')
    native=(stage=='asr' and provider in {'faster_whisper','fun_asr'} or stage=='tts' and provider=='indextts'
            or stage=='translation' and not uses_translation_service(settings))
    if native:
        ready=model_readiness(settings)[stage+'_ready']
        if stage=='asr' and provider=='fun_asr' and not ready:
            return dict(ok=False,level='files',message='Fun-ASR 环境或模型未准备，请先运行 .venv/Scripts/python.exe scripts/prepare_fun_asr.py')
        return dict(ok=ready,level='files',message='本地文件检查通过；未加载模型' if ready else '缺少本地模型文件或运行环境，请检查路径')
    url=getattr(settings,stage+'_url');model=getattr(settings,stage+'_name')
    try:
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=5) as client:
            response=client.get(url+'/models')
        if response.status_code in {404,405}:
            return dict(ok=True,level='reachable',message='服务可达，但未提供模型列表；尚未验证推理能力')
        check_response(response)
        data=response.json().get('data',[])
        ids=[item['id'] for item in data if isinstance(item,dict) and isinstance(item.get('id'),str)]
        return dict(ok=model in ids,level='models',models=ids[:100],message='服务可达且模型已列出；尚未验证推理能力' if model in ids else '服务可达，但模型列表没有配置的模型 ID')
    except (httpx.HTTPError,ValueError,TypeError,AttributeError):
        return dict(ok=False,level='connection',message='无法验证本地模型服务，请检查地址、接口协议、模型 ID 及服务是否已启动')
