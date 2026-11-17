"""On-demand source audio excerpts for subtitle checking; never writes project metadata."""
from __future__ import annotations
import hashlib
import json
import math
import os
import threading
import uuid
import wave
from array import array
import sys
from filelock import FileLock
from .media import extract_audio,wav_duration

def source_audio_clip(store,identifier,segment_id,settings,*,start=None,end=None,context=0):
    project=store.read(identifier)
    row=next((row for row in project['segments'] if row['id']==segment_id),None)
    if row is None:raise ValueError('当前项目中找不到这句字幕，请刷新后重试')
    duration=float(project['media']['duration'])
    start=float(row['start'] if start is None else start);end=float(row['end'] if end is None else end);context=float(context)
    if not all(math.isfinite(v) for v in [start,end,context,duration]) or not 0<=context<=2:raise ValueError('无效的原声试听时间')
    if start<0 or end<=start or end>duration+.001:raise ValueError('试听时间必须在原片范围内，且结束时间晚于开始时间')
    if end-start>300:raise ValueError('单次原声试听最多 5 分钟，请缩小时间范围')
    if project['media'].get('audio') is False:raise ValueError('原视频中没有可播放的音轨')
    actual_start=max(0,start-context);actual_end=min(duration,end+context)
    folder=store.folder(identifier);source=folder/'source.media';stat=source.stat()
    identity=[1,stat.st_size,stat.st_mtime_ns,actual_start,actual_end,24000]
    key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    cache=folder/'cache';cache.mkdir(exist_ok=True)
    target=cache/('source-cue-'+key+'.wav')
    with FileLock(cache/('source-cue-'+key+'.lock'),timeout=20):
        if target.is_file():
            try:
                if 0<wav_duration(target)<=actual_end-actual_start+.1:return target,actual_start,actual_end
            except (OSError,ValueError,EOFError,wave.Error):pass
        pending=cache/('source-cue-'+key+'-'+uuid.uuid4().hex+'.pending.wav')
        try:
            extract_audio(source,pending,settings,cache/('source-cue-'+key+'.log'),threading.Event(),start=actual_start,duration=actual_end-actual_start)
            if wav_duration(pending)<=0:raise ValueError('这一段没有可播放的原声音频，请调整时间后重试')
            current=source.stat()
            if (current.st_size,current.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):raise ValueError('原视频已更新，请重新试听')
            os.replace(pending,target)
        finally:pending.unlink(missing_ok=True)
    return target,actual_start,actual_end


def source_waveform(store, identifier, segment_id, settings, *, start=None, end=None):
    path, begin, finish = source_audio_clip(store, identifier, segment_id, settings, start=start, end=end)
    with wave.open(str(path), 'rb') as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            raise ValueError('原声波形需要单声道 PCM 音频，请重新生成试听缓存')
        count = stream.getnframes()
        width = max(1, math.ceil(count / 1000))
        peaks = []
        while True:
            raw = stream.readframes(width)
            if not raw:
                break
            values = array('h', raw)
            if sys.byteorder != 'little':
                values.byteswap()
            peaks.append(round(max(abs(v) for v in values) / 32768, 4))
    return dict(start=begin, end=finish, peaks=peaks)
