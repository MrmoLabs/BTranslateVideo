"""Read-only access to completed speech. Never launches TTS or rewrites drafts."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import uuid
import wave
from pathlib import Path
from filelock import FileLock
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from .languages import target_code
from .project_storage import plain_path
from .subtitles import validate_segments


def voice_identity(row):
    return [row.get('id'),str(row.get('zh','')).strip(),row.get('speaker'),row.get('emotion','inherit'),
            'medium' if row.get('emotion','inherit')=='inherit' else row.get('emotion_strength','medium')]


def wav_info(path):
    try:
        with wave.open(str(path),'rb') as stream:
            frames=stream.getnframes();rate=stream.getframerate();width=stream.getnchannels()*stream.getsampwidth()
            if not frames or not rate or not width or frames*width>path.stat().st_size:return None
            stream.setpos(frames-1)
            if len(stream.readframes(1))!=width:return None
            return dict(frames=frames,rate=rate,duration=frames/rate)
    except (FileNotFoundError,ValueError,EOFError,wave.Error):return None


def local_file(folder,name):
    path=folder/name
    plain_path(path,folder)
    return path


def materialize(folder,source,start=None,end=None):
    plain_path(source,folder);info=wav_info(source)
    if not info:return None
    first=0 if start is None else round(start*info['rate'])
    finish=info['frames'] if end is None else round(end*info['rate'])
    if first<0 or finish<=first or finish>info['frames']+1:return None
    finish=min(finish,info['frames'])
    if finish<=first:return None
    if (finish-first)/info['rate']>300:return None
    stat=source.stat();version=(stat.st_size,stat.st_mtime_ns)
    key=hashlib.sha256(json.dumps([1,source.relative_to(folder).as_posix(),*version,first,finish]).encode()).hexdigest()[:24]
    cache=folder/'cache';plain_path(cache,folder);cache.mkdir(exist_ok=True)
    target=cache/('generated-cue-'+key+'.wav');plain_path(target,folder)
    with FileLock(cache/('generated-cue-'+key+'.lock'),timeout=3):
        if wav_info(target):return dict(token=key,duration=(finish-first)/info['rate'])
        temporary=cache/('generated-cue-'+key+'-'+uuid.uuid4().hex+'.pending.wav')
        try:
            with wave.open(str(source),'rb') as original,wave.open(str(temporary),'wb') as output:
                output.setparams(original.getparams());original.setpos(first);remaining=finish-first
                while remaining:
                    count=min(remaining,65536);payload=original.readframes(count)
                    if len(payload)!=count*original.getsampwidth()*original.getnchannels():raise ValueError('已生成音频不完整，请重新生成本句')
                    output.writeframes(payload);remaining-=count
            latest=source.stat()
            if (latest.st_size,latest.st_mtime_ns)!=version:raise ValueError('音轨已更新，请重新打开本句')
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
    return dict(token=key,duration=(finish-first)/info['rate'])


def clip_index(folder):
    path=folder/'cache'/'voice-clips.json';plain_path(path,folder)
    try:
        if path.stat().st_size>20*1024**2:return {}
        value=json.loads(path.read_text(encoding='utf-8'))
        return value.get('items',{}) if isinstance(value,dict) and isinstance(value.get('items'),dict) else {}
    except (FileNotFoundError,ValueError):return {}


def remember_clips(store,identifier,rows,paths,language):
    """Derived index for the latest completed clip per sentence; audio stays authoritative."""
    from .file_io import atomic_json
    folder=store.folder(identifier)
    with store.lock:
        items=clip_index(folder)
        for row,path in zip(rows,paths):
            plain_path(path,folder)
            relative=path.relative_to(folder).as_posix()
            if re.fullmatch(r'cache/tts-[0-9a-f]{24}\.wav',relative) and wav_info(path):
                items[str(row['id'])]=dict(segment={key:row[key] for key in ('id','start','end','text','zh','speaker','emotion','emotion_strength') if key in row},file=relative,target_language=language)
        # Do not retain entries for sentence IDs removed by resegmentation.
        ids={str(row['id']) for row in store.read(identifier)['segments']}
        atomic_json(folder/'cache'/'voice-clips.json',dict(version=1,items={key:value for key,value in items.items() if key in ids}))


def remember_clips_safely(store,identifier,rows,paths,language):
    try:remember_clips(store,identifier,rows,paths,language)
    except (OSError,ValueError) as exc:
        # A derived playback index must not abort completed speech synthesis.
        try:store.log(identifier,'语音已生成，但播放索引未更新：'+str(exc))
        except OSError:pass


def resolve_speech(store,identifier,draft,settings,*,busy=False,expected_revision=None):
    from .pipeline import make_voice_clip,file_hash,dub_signature
    from .model_backends import tts_backend,tts_model_signature
    project=store.read(identifier);folder=store.folder(identifier)
    if expected_revision is not None and expected_revision!=project.get('edit_revision',0):raise HTTPException(409,'字幕版本已更新，当前草稿保留，请重新打开项目后再听')
    row=next((item for item in project['segments'] if item['id']==draft.get('id')),None)
    if row is None:raise ValueError('当前项目中找不到这句字幕')
    try:current=validate_segments([draft],project['media']['duration'])[0]
    except (ValueError,KeyError,TypeError):current=None
    candidates=[];raw={};model=None;legacy_window=None
    def cached(item):
        nonlocal model
        if not item or not str(item.get('zh','')).strip():return None
        identity=json.dumps(voice_identity(item),ensure_ascii=False)
        if identity in raw:return raw[identity]
        try:
            reference=(project.get('references') or {}).get(item['speaker'],'')
            if tts_backend(settings).reference_audio and not reference:return None
            if reference:local_file(folder,reference)
            if model is None:model=tts_model_signature(settings)
            clip=make_voice_clip(item,folder,reference,settings,model,file_hash(folder/reference) if reference else '',target_code(project))
            path=Path(clip['output']);plain_path(path,folder)
            value=(path,clip)
            raw[identity]=value;return value
        except (ValueError,FileNotFoundError):return None
    def add_raw(item):
        found=cached(item)
        if found:candidates.append(dict(kind='cache',path=found[0],segment=item,settings_current=True))
    # An edited draft may already have been synthesized in an earlier audition.
    changed=current is not None and voice_identity(current)!=voice_identity(row)
    if changed:add_raw(current)
    audio=project.get('outputs',{}).get('audio')
    if audio and Path(audio).name==audio and not busy:
        timings=project.get('dub_timing') or []
        slot=next((item for item in timings if item.get('id')==row['id']),None)
        start,end=(slot['start'],slot['end']) if slot else (row['start'],row['end'])
        if all(isinstance(value,(int,float)) and math.isfinite(value) for value in (start,end)):
            try:
                for reference in project.get('references',{}).values():
                    if reference:local_file(folder,reference)
                settings_current=bool(project.get('dub_signature')) and project['dub_signature']==dub_signature(project,folder,settings)
            except (ValueError,FileNotFoundError):settings_current=False
            candidate=dict(kind='timeline' if slot else 'timeline_window',path=local_file(folder,audio),segment=row,start=start,end=end,settings_current=settings_current)
            if slot:candidates.append(candidate)
            else:legacy_window=candidate
    if not changed:add_raw(current)
    add_raw(row)
    previous=clip_index(folder).get(str(row['id']))
    if isinstance(previous,dict) and isinstance(previous.get('segment'),dict) and re.fullmatch(r'cache/tts-[0-9a-f]{24}\.wav',previous.get('file','')):
        old=previous['segment'];found=cached(old)
        same=bool(found and found[0].relative_to(folder).as_posix()==previous['file'] and previous.get('target_language')==target_code(project))
        candidates.append(dict(kind='previous_cache',path=local_file(folder,previous['file']),segment=old,settings_current=same,target_language=previous.get('target_language','zh')))
    # Older installations already saved the immediately preceding subtitle revision.
    previous_file=local_file(folder,'segments.previous.json')
    if previous_file.is_file() and previous_file.stat().st_size<=20*1024**2:
        try:
            previous_rows=json.loads(previous_file.read_text(encoding='utf-8'))
            old=next((item for item in previous_rows if isinstance(item,dict) and item.get('id')==row['id']),None) if isinstance(previous_rows,list) else None
            if old:add_raw(old)
        except ValueError:pass
    sample=project.get('audition') or {};sample_row=sample.get('segment') or {};name=sample.get('file','')
    if sample_row.get('id')==row['id'] and re.fullmatch(r'audition-[0-9a-f]{24}\.wav',name):
        found=cached(sample_row)
        same=bool(found and name=='audition-'+found[0].stem.removeprefix('tts-')+'.wav')
        candidates.append(dict(kind='audition',path=local_file(folder,name),segment=sample_row,settings_current=same))
    if legacy_window:candidates.append(legacy_window)
    for item in candidates:
        value=materialize(folder,item['path'],item.get('start'),item.get('end'))
        if value:
            return dict(available=True,kind=item['kind'],segment=item['segment'],settings_current=item['settings_current'],
                        start=item.get('start'),end=item.get('end'),target_language=item.get('target_language',target_code(project)),**value)
    return dict(available=False)


class ExistingSpeech(BaseModel):
    segment:dict
    revision:int=Field(ge=0)


def attach(app,store,pipeline):
    from .config import load_settings
    from filelock import Timeout
    @app.post('/api/projects/{identifier}/generated-speech')
    def existing(identifier:str,data:ExistingSpeech):
        try:result=resolve_speech(store,identifier,data.segment,load_settings(),busy=pipeline.busy(identifier),expected_revision=data.revision)
        except Timeout:raise HTTPException(503,'已生成音频片段正在准备，请稍后重试')
        if result.get('available'):result['url']=f"/api/projects/{identifier}/generated-speech/{result['token']}.wav"
        return result

    @app.get('/api/projects/{identifier}/generated-speech/{filename}')
    def media(identifier:str,filename:str):
        store.read(identifier)
        if not re.fullmatch(r'[0-9a-f]{24}\.wav',filename):raise HTTPException(404,'音频不可访问')
        path=local_file(store.folder(identifier),'cache/generated-cue-'+filename)
        if not path.is_file():raise HTTPException(404,'音频片段已失效，请重新打开本句')
        return FileResponse(path,media_type='audio/wav',headers={'Cache-Control':'private, no-store'})
