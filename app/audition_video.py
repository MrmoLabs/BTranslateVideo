"""Bounded single-cue video simulation, without mutating the project timeline."""
from __future__ import annotations

import hashlib
import json
import os

from .alignment import plan_alignment
from .media import check_cancel, ffmpeg, fit_clip, mix_timeline, probe, wav_duration
from .subtitles import render_vtt


def make_audition_video(project, folder, row, raw, settings, cancel, progress):
    source=folder/'source.media'
    if not project['media'].get('video'):raise ValueError('此项目没有可预览的视频画面')
    if row['end']-row['start']>300:raise ValueError('单句视频预览最长 5 分钟，请缩短这句的时间范围')
    rows=project['segments'];index=next(i for i,item in enumerate(rows) if item['id']==row['id'])
    previous=rows[index-1]['end'] if index else 0
    following=rows[index+1]['start'] if index+1<len(rows) else project['media']['duration']
    earliest=max(0,row['start']-min(settings.max_lead,max(0,row['start']-previous)))
    latest=min(project['media']['duration'],row['end']+min(settings.max_lag,max(0,following-row['end'])))
    local={**row,'start':row['start']-earliest,'end':row['end']-earliest}
    plan=plan_alignment([local],[wav_duration(raw)],latest-earliest,settings)[0]
    stat=source.stat()
    identity=dict(schema=1,policy='single-cue-natural-short',source=[stat.st_size,stat.st_mtime_ns],row=row,raw=raw.name,plan=plan,bounds=[earliest,latest],speed=settings.max_speed)
    key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    name=f'audition-video-{key}.mp4';captions=f'audition-video-{key}.vtt'
    fitted=folder/'cache'/f'audition-fit-{key}.wav'
    timeline=folder/'cache'/f'audition-track-{key}.wav'
    pending=folder/f'audition-video-{key}.pending.mp4'
    pending_vtt=folder/f'audition-video-{key}.pending.vtt'
    log=folder/'process.log'
    try:
        progress(88,'正在对齐本句配音与原片画面',dict(phase='audition_video_align'))
        speed=fit_clip(raw,fitted,plan['window'],settings,log,cancel)
        voice_start=earliest+plan['start'];voice_duration=wav_duration(fitted)
        begin=min(row['start'],voice_start);end=max(row['end'],voice_start+voice_duration)
        if end>latest+.0001:raise ValueError('本句配音超出允许时间，未截断，请缩短配音稿或调整时间')
        duration=end-begin;check_cancel(cancel)
        metadata=dict(video=name,captions=captions,start=begin,end=end,voice_start=voice_start,voice_duration=voice_duration,speed=speed,audio_mode='voice_only')
        if (folder/name).is_file() and (folder/captions).is_file():
            try:info=probe(folder/name,settings)
            except ValueError:info={}
            if info.get('video') and info.get('audio') and abs(info['duration']-duration)<.2:return metadata
        mix_timeline([(voice_start-begin,fitted)],duration,timeline,cancel)
        def tick(done,total):progress(min(99,90+int(9*done/total)),f'本句视频预览：{done:.1f}/{total:.1f} 秒',dict(phase='audition_video_encode',done=done,total=total,unit='seconds'))
        # Decode the original container directly: an uploaded MKV need not be browser-playable.
        ffmpeg(settings,['-ss',str(begin),'-i',str(source),'-i',str(timeline),'-map','0:v:0','-map','1:a:0','-t',str(duration),'-sn','-dn','-vf',"scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,format=yuv420p",'-c:v','libx264','-preset','veryfast','-crf','24','-c:a','aac','-b:a','128k','-ar','48000','-movflags','+faststart',str(pending)],log,cancel,duration=duration,on_progress=tick)
        check_cancel(cancel);info=probe(pending,settings)
        if not info['video'] or not info['audio'] or abs(info['duration']-duration)>.2:raise ValueError('本句视频预览时长或音轨异常，未替换之前的试听')
        caption={**row,'start':max(0,voice_start-begin),'end':min(duration,voice_start-begin+voice_duration)}
        pending_vtt.write_text(render_vtt([caption]),encoding='utf-8',newline='\n')
        if source.stat().st_mtime_ns!=stat.st_mtime_ns or source.stat().st_size!=stat.st_size:raise ValueError('原视频已变更，请重新生成预览')
        check_cancel(cancel);os.replace(pending,folder/name);os.replace(pending_vtt,folder/captions)
        return metadata
    finally:
        pending.unlink(missing_ok=True);pending_vtt.unlink(missing_ok=True)
        fitted.unlink(missing_ok=True);timeline.unlink(missing_ok=True)
