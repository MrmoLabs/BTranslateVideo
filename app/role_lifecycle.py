"""Stable role reuse based on this project's timeline, never label/name identity."""
from __future__ import annotations
import re
from bisect import bisect_right
import unicodedata
from .project_roles import project_roles


def name_key(name):return ' '.join(unicodedata.normalize('NFKC',name).split()).casefold()


def role_name(project,speaker):
    return project.get('roles',{}).get(speaker,{}).get('name') or (f'角色 {speaker[8:]}' if re.fullmatch(r'speaker_\d+',speaker) else speaker)


def unique_role_name(project,prefix='说话人',reserved=()):
    used={name_key(role_name(project,speaker)) for speaker in project_roles(project)}|{name_key(name) for name in reserved}
    i=1
    while name_key(f'{prefix} {i}') in used:i+=1
    return f'{prefix} {i}'


def ensure_unique_name(project,name,speaker=None):
    if any(other!=speaker and name_key(role_name(project,other))==name_key(name) for other in project_roles(project)):
        raise ValueError('项目中已有同名角色，请使用不同名称，或删除 / 合并重复角色')


def reused_roles(project,exclusive,labels):
    """Require dominant, mutual timeline overlap; do not equate pyannote labels.

    Reference windows provide a stronger anchor for older duplicate roles and
    partially adopted runs. Only previous diarization roles are auto-reused;
    mapping a manually created role is an explicit user choice.
    """
    candidates={speaker for speaker in project_roles(project) if project.get('roles',{}).get(speaker,{}).get('origin')=='diarization' or project.get('roles',{}).get(speaker,{}).get('diarization_linked')
                or (speaker.startswith('diar_') and project.get('reference_origins',{}).get(speaker,{}).get('kind')=='diarization')}
    if not candidates:return {}
    anchors=[];votes={};totals={label:0. for label in labels}
    ends=[turn['end'] for turn in exclusive]
    def overlaps(start,end):
        for index in range(bisect_right(ends,start),len(exclusive)):
            turn=exclusive[index]
            if turn['start']>=end:break
            seconds=max(0,min(end,turn['end'])-max(start,turn['start']))
            if seconds:yield turn['speaker'],seconds
    for speaker in candidates:
        origin=project.get('reference_origins',{}).get(speaker,{})
        start=origin.get('start');duration=origin.get('duration')
        if not isinstance(start,(int,float)) or not isinstance(duration,(int,float)) or duration<3:continue
        weights={}
        for label,overlap in overlaps(start,start+duration):weights[label]=weights.get(label,0)+overlap
        if not weights:continue
        label,overlap=max(weights.items(),key=lambda item:(item[1],item[0]))
        if overlap/duration>=.85 and overlap>=max((weight for key,weight in weights.items() if key!=label),default=0)*3:
            anchors.append((overlap/duration,overlap,speaker,label))
    # Prefer a role still assigned more dialogue when historical duplicates
    # contain the same reference; this is deterministic and one-to-one.
    usage={speaker:sum(r['end']-r['start'] for r in project['segments'] if r['speaker']==speaker) for speaker in candidates}
    # For roles without a reference, require at least 3 seconds and 80% purity
    # on both sides. A split/merge does not silently claim the old identity.
    role_totals={speaker:0. for speaker in candidates}
    for row in project['segments']:
        speaker=row['speaker']
        for label,overlap in overlaps(row['start'],row['end']):
            totals[label]+=overlap
            if speaker not in candidates:continue
            key=(label,speaker);votes[key]=votes.get(key,0)+overlap
            role_totals[speaker]+=overlap
    windows={}
    for _,_,speaker,label in anchors:
        origin=project.get('reference_origins',{}).get(speaker,{})
        windows.setdefault(label,set()).add((round(origin['start'],6),round(origin['duration'],6)))
    ambiguous={label for label,positions in windows.items() if len(positions)>1}
    matches={};used=set()
    for score,overlap,speaker,label in sorted(anchors,key=lambda v:(-v[0],-usage[v[2]],v[2],v[3])):
        if label in ambiguous:continue
        if label not in matches and speaker not in used:matches[label]=speaker;used.add(speaker)
    for (label,speaker),overlap in sorted(votes.items(),key=lambda v:(-v[1],v[0])):
        if label in ambiguous or label in matches or speaker in used:continue
        if overlap>=3 and overlap>=.8*totals[label] and overlap>=.8*role_totals[speaker]:matches[label]=speaker;used.add(speaker)
    return matches
