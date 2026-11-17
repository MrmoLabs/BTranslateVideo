"""Project roles and conservative reference-window selection."""
from __future__ import annotations

UNASSIGNED_SPEAKER='__unassigned__'

def project_roles(project):
    return (set(project.get('roles',{}))|{row['speaker'] for row in project['segments']}|set(project.get('references',{})))-{UNASSIGNED_SPEAKER}

def reference_window(project,speaker):
    intervals=[]
    for row in sorted(project['segments'],key=lambda r:r['start']):
        if row['speaker']!=speaker:continue
        start,end=row['start'],row['end']
        if intervals and start-intervals[-1][1]<=.35:intervals[-1][1]=max(end,intervals[-1][1])
        else:intervals.append([start,end])
    # Remove other assigned speakers, including overlapping speech and short turns
    # inside the small gaps merged above. This does not perform speaker diarization.
    for other in project['segments']:
        if other['speaker']==speaker:continue
        pieces=[]
        for start,end in intervals:
            if other['end']<=start or other['start']>=end:pieces.append([start,end]);continue
            if other['start']>start:pieces.append([start,min(end,other['start'])])
            if other['end']<end:pieces.append([max(start,other['end']),end])
        intervals=pieces
    candidates=[(start,min(10,end-start)) for start,end in intervals if end-start>=3]
    if not candidates:raise ValueError('这个角色没有至少 3 秒且无其他角色重叠的台词片段；请先分配连续台词，或上传参考音')
    start,duration=max(candidates,key=lambda value:(value[1],-value[0]))
    return dict(start=start,duration=duration,end=start+duration)
