"""Measured task progress. Unknown work is indeterminate, never a simulated percent."""
from __future__ import annotations
import copy
import math
import time
import uuid

ACTIVE={'queued','running','cancelling'}
TERMINAL={'done','failed','cancelled','interrupted'}


def new_progress(action, steps=None, *, at=None):
    now=time.time() if at is None else at
    steps=steps or (['analyze','translate','dub','export'] if action=='all' else ['analyze' if action in {'resegment','retranscribe'} else action])
    return dict(id=uuid.uuid4().hex,action=action,status='queued',steps=steps,step=steps[0],completed_steps=[],
                queued_at=now,started_at=None,finished_at=None,updated_at=now,phase_started_at=None,
                phase='queued',message='排队中',done=None,total=None,unit='',reused=0,current='',samples=[],events=[])


def advance(value, *, phase=None, message=None, step=None, done=None, total=None, unit='', reused=None,
            current=None, status=None, at=None):
    result=copy.deepcopy(value);now=time.time() if at is None else at
    if result['status'] in TERMINAL:return result
    if result['status']=='queued' and status in {None,'running'}:
        result.update(status='running',started_at=now)
    if status:result['status']=status
    phase=phase or result['phase'];step=step or result['step']
    changed_phase=phase!=result['phase'] or step!=result['step']
    if step!=result['step'] and result['step'] in result['steps']:
        result['completed_steps']=list(dict.fromkeys(result['completed_steps']+[result['step']]))
    if changed_phase:
        result.update(phase_started_at=now,done=None,total=None,unit='',samples=[])
        result.pop('eta_seconds',None)
    result.update(phase=phase,step=step)
    if message is not None:result['message']=str(message)[:1000]
    if current is not None:result['current']=str(current)[:200]
    if reused is not None:result['reused']=max(0,int(reused))
    if total is not None:
        try:valid=math.isfinite(float(total)) and float(total)>0 and done is not None and math.isfinite(float(done))
        except (TypeError,ValueError):valid=False
        if valid:
            count=min(float(total),max(0,float(done)));total=float(total)
            if result['total']!=total or result['unit']!=unit:result['samples']=[]
            if (phase not in {'tts_generate','tts_service'} or count>0) and (not result['samples'] or count>result['samples'][-1][1]):
                result['samples']=(result['samples']+[[now,count]])[-8:]
            result.update(done=count,total=total,unit=unit)
            samples=result['samples'];result.pop('eta_seconds',None)
            if len(samples)>=3 and samples[-1][0]-samples[0][0]>=2 and count>samples[0][1] and count<total:
                result['eta_seconds']=round((total-count)*(samples[-1][0]-samples[0][0])/(count-samples[0][1])*1.2)
    semantic=('phase','step','message','done','total','unit','reused','current','status')
    if any(result.get(k)!=value.get(k) for k in semantic):
        result['updated_at']=now
        event=dict(time=now,message=result['message'],phase=phase)
        if not result['events'] or result['events'][-1]['message']!=event['message']:
            result['events']=(result['events']+[event])[-8:]
    return result


def finish(value,status,*,message=None,at=None):
    result=copy.deepcopy(value);now=time.time() if at is None else at
    if status not in TERMINAL:raise ValueError('Invalid terminal progress status')
    result.update(status=status,finished_at=now,updated_at=now)
    if message:
        result['message']=str(message)[:1000]
        result['events']=(result['events']+[dict(time=now,message=result['message'],phase=result['phase'])])[-8:]
    result.pop('eta_seconds',None)
    if status=='done':result['completed_steps']=list(result['steps'])
    return result


def tts_detail(runtime):
    phase=runtime.get('phase','正在合成')
    measured=runtime.get('actual_batch',0)>0 or runtime.get('completed',0)>0 or phase=='全部复用缓存'
    total=runtime.get('total',0)
    code='tts_service' if phase=='本地服务合成' else 'tts_cached' if phase=='全部复用缓存' else 'tts_generate' if measured else 'tts:'+phase
    return dict(phase=code,message=phase,done=runtime.get('completed',0) if measured else None,
                total=total if measured else None,unit='segments',reused=runtime.get('reused',0))
