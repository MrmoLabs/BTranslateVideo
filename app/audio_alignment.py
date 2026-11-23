"""Bounded, observable alignment recovery using existing speech, never text rewriting."""
from __future__ import annotations
import hashlib
import json
import math
import os
import uuid
import wave
from pathlib import Path
from .alignment import plan_alignment
from .media import check_cancel,wav_duration
from .file_io import atomic_json

EDGE_VERSION=1


class AlignmentBlocked(ValueError):
    def __init__(self,report):
        self.report=report
        super().__init__(f"语音缓存已保留；{len(report['conflicts'])} 句存在时间冲突。请在配音页筛选‘只看对齐问题’后批量调整，不必重新生成全部语音。")


def quiet_edges(source):
    """Only extended near-silent edges; protect 100/120 ms around the signal.

    -72 dBFS RMS is intentionally conservative. Never remove internal pauses,
    trim an all-quiet clip, or modify the source. Read only two seconds per edge.
    """
    import numpy as np
    with wave.open(str(source),'rb') as stream:
        rate=stream.getframerate();frames=stream.getnframes();channels=stream.getnchannels()
        duration=frames/rate
        if stream.getsampwidth()!=2 or channels!=1:return duration,0,0
        count=min(frames,rate*2);head=np.frombuffer(stream.readframes(count),'<i2').astype(float)
        stream.setpos(max(0,frames-count));tail=np.frombuffer(stream.readframes(count),'<i2').astype(float)
    step=max(1,round(rate*.005));lead=last=0;head_active=False;tail_active=False
    for index in range(0,len(head),step):
        if np.sqrt(np.mean(head[index:index+step]**2))>8:head_active=True;break
        lead=min(index+step,len(head))/rate
    for index in range(len(tail),0,-step):
        if np.sqrt(np.mean(tail[max(0,index-step):index]**2))>8:tail_active=True;break
        last=(len(tail)-max(0,index-step))/rate
    if not head_active or not tail_active:return duration,0,0
    lead=max(0,lead-.1) if lead>=.2 else 0
    last=max(0,last-.12) if last>=.22 else 0
    if lead+last>duration-.4:lead=last=0
    return duration,round(lead*rate),round(last*rate)


def edge_clip(source,folder,cancel):
    check_cancel(cancel);source=Path(source);stat=source.stat()
    identity=[EDGE_VERSION,source.name,stat.st_size,stat.st_mtime_ns]
    key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    metadata=folder/('edge-'+key+'.json');target=folder/('edge-'+key+'.wav')
    try:
        value=json.loads(metadata.read_text(encoding='utf-8'))
        if value.get('identity')==identity:
            if not value['lead_frames'] and not value['tail_frames']:return source,value
            if target.is_file() and abs(wav_duration(target)-value['duration'])<1/value['rate']:return target,value
    except (FileNotFoundError,ValueError,KeyError,EOFError,wave.Error):pass
    duration,lead,last=quiet_edges(source)
    with wave.open(str(source),'rb') as original:
        rate=original.getframerate();total=original.getnframes()
        value=dict(identity=identity,raw_duration=duration,lead_frames=lead,tail_frames=last,rate=rate,duration=(total-lead-last)/rate)
        if lead or last:
            temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.pending.wav')
            try:
                with wave.open(str(temporary),'wb') as output:
                    output.setparams(original.getparams());original.setpos(lead);remaining=total-lead-last
                    while remaining:
                        check_cancel(cancel);count=min(remaining,65536);data=original.readframes(count)
                        if len(data)!=count*original.getnchannels()*original.getsampwidth():raise ValueError('语音缓存不完整')
                        output.writeframes(data);remaining-=count
                current=source.stat()
                if (current.st_size,current.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):raise ValueError('语音缓存已变化，请重试')
                os.replace(temporary,target)
            finally:temporary.unlink(missing_ok=True)
    atomic_json(metadata,value)
    return (target if lead or last else source),value


def conflict_report(rows,durations,video_duration,settings):
    """All proven earliest-start deadline violations, plus immediate neighbours."""
    records = []
    affected = {}
    previous = 0
    overlap=any(a['end']>b['start']+.00001 for a,b in zip(rows,rows[1:]))
    for index,(row,duration) in enumerate(zip(rows,durations)):
        if duration <= 0:
            records.append(None)
            continue
        normal_end=max(row['end'],rows[index+1]['start'] if index+1<len(rows) else video_duration)
        earliest=max(0,row['start']-settings.max_lead)
        reserve = 0 if overlap else min(.12, .04 + duration * .002)
        minimum = duration / settings.max_speed + reserve
        start=max(previous,earliest) if not overlap else row['start']
        end_limit=min(video_duration,normal_end+settings.max_lag)
        if overlap:end_limit=min(video_duration,normal_end)
        late=max(0,start-(row['start']+settings.max_lag))
        short=max(0,start+minimum-end_limit)
        capacity = max(0, (end_limit - start - reserve) * settings.max_speed)
        records.append(dict(id=row['id'], voice_seconds=round(duration, 3),
                            window_seconds=round(row['end'] - row['start'], 3),
                            required_speed=round(duration / max(.001, row['end'] - row['start']), 3),
                            max_voice_seconds=math.floor(capacity * 1000) / 1000))
        if late>.00001 or short>.00001:
            for neighbour in range(max(0,index-1),min(len(rows),index+2)):
                if neighbour == index or neighbour not in affected:
                    affected[neighbour] = (round(max(late, short), 3), 'sequence_pressure' if neighbour == index else 'neighbour')
        previous=start+minimum
    return [{**record, 'pressure_seconds': affected[index][0], 'reason': affected[index][1]}
            for index, record in enumerate(records) if record is not None and index in affected]


def prepare_alignment(rows,clips,video_duration,settings,folder,cancel,progress):
    if len(rows)!=len(clips) or not rows:raise ValueError('配音时长与字幕段数不一致')
    raw=[wav_duration(path) for path in clips]
    if any(not math.isfinite(d) or d<=0 for d in raw):raise ValueError('配音片段时长无效')
    normal=dict(speed=settings.max_speed,lead=settings.max_lead,lag=settings.max_lag)
    report=dict(version=1,status='planning',mode=settings.alignment_mode,normal=normal,total=len(rows),trimmed_count=0,trimmed_seconds=0,conflicts=[])
    empty=[dict(raw_duration=d,duration=d,lead_frames=0,tail_frames=0,rate=24000) for d in raw]
    def finish(plan,paths,durations,used,edges,strategy):
        report.update(status='ready',strategy=strategy,effective=dict(speed=used.max_speed,lead=used.max_lead,lag=used.max_lag),
            over_normal_count=sum(d/slot['window']>settings.max_speed+.00001 for d,slot in zip(durations,plan)),
            max_speed_used=round(max(max(1,d/slot['window']) for d,slot in zip(durations,plan)),3),
            max_advance_used=round(max(max(0,r['start']-slot['start']) for r,slot in zip(rows,plan)),3),
            max_delay_used=round(max(max(0,slot['start']-r['start']) for r,slot in zip(rows,plan)),3))
        return plan,paths,durations,used,edges,report
    try:return finish(plan_alignment(rows,raw,video_duration,settings),clips,raw,settings,empty,'normal')
    except ValueError as original:
        report['normal_error']=str(original)
    if settings.alignment_mode=='strict':
        report.update(status='blocked',effective=normal,conflicts=conflict_report(rows,raw,video_duration,settings));raise AlignmentBlocked(report)
    cleaned=[];edges=[]
    for index,path in enumerate(clips):
        check_cancel(cancel);prepared,metadata=edge_clip(path,folder,cancel);cleaned.append(prepared);edges.append(metadata)
        if index%50==0 or index+1==len(clips):progress(index+1,len(clips),'检查并保留完整语音，收紧两端低音量静音')
    durations=[item['duration'] for item in edges]
    report.update(trimmed_count=sum(bool(item['lead_frames'] or item['tail_frames']) for item in edges),trimmed_seconds=round(sum(raw)-sum(durations),3))
    try:return finish(plan_alignment(rows,durations,video_duration,settings),cleaned,durations,settings,edges,'quiet_edges')
    except ValueError:pass
    cap=max(settings.max_speed,settings.alignment_fallback_speed)
    lead=max(settings.max_lead,settings.alignment_fallback_lead);lag=max(settings.max_lag,settings.alignment_fallback_lag)
    # First use extra silence at the existing speed limit, then increase by
    # at most 0.05 per attempt. The configured fallback is a hard upper bound.
    speeds=sorted(set([settings.max_speed,cap]+[round(settings.max_speed+.05*i,8) for i in range(1,21) if settings.max_speed+.05*i<cap]))
    effective=settings
    for speed in speeds:
        check_cancel(cancel);effective=settings.model_copy(update=dict(max_speed=speed,max_lead=lead,max_lag=lag))
        try:return finish(plan_alignment(rows,durations,video_duration,effective),cleaned,durations,effective,edges,'adaptive')
        except ValueError:pass
    report.update(status='blocked',effective=dict(speed=cap,lead=lead,lag=lag),conflicts=conflict_report(rows,durations,video_duration,effective))
    raise AlignmentBlocked(report)


def preflight_cached(rows,clips,video_duration,settings,folder,cancel):
    """An optimistic bound: unknown clips take zero time, so only proven
    conflicts in already generated speech can stop an expensive new TTS run.
    """
    known=[path.is_file() for path in clips]
    if not any(known) or all(known):return None
    durations=[]
    for path,ready in zip(clips,known):
        check_cancel(cancel)
        if not ready:durations.append(0);continue
        if settings.alignment_mode=='adaptive':_,edge=edge_clip(path,folder,cancel);durations.append(edge['duration'])
        else:durations.append(wav_duration(path))
    effective=settings if settings.alignment_mode=='strict' else settings.model_copy(update=dict(
        max_speed=max(settings.max_speed,settings.alignment_fallback_speed),max_lead=max(settings.max_lead,settings.alignment_fallback_lead),max_lag=max(settings.max_lag,settings.alignment_fallback_lag)))
    conflicts=conflict_report(rows,durations,video_duration,effective)
    if not conflicts:return None
    return dict(version=1,status='blocked',strategy='cached_preflight',mode=settings.alignment_mode,total=len(rows),cached_count=sum(known),pending_count=len(rows)-sum(known),
                normal=dict(speed=settings.max_speed,lead=settings.max_lead,lag=settings.max_lag),effective=dict(speed=effective.max_speed,lead=effective.max_lead,lag=effective.max_lag),
                trimmed_count=0,trimmed_seconds=0,conflicts=conflicts)
