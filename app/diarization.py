"""Local diarization proposals: inference never rewrites project roles by itself."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from bisect import bisect_right
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .config import ROOT, Settings, atomic_json, resolve
from .media import check_cancel, extract_audio, run
from .project_roles import reference_window
from .role_lifecycle import reused_roles,role_name,unique_role_name

MODEL_FILES = ('config.yaml', 'segmentation/pytorch_model.bin',
               'embedding/pytorch_model.bin', 'plda/plda.npz', 'plda/xvec_transform.npz')


class DiarizationOptions(BaseModel):
    device: str = 'auto'
    num_speakers: int | None = Field(default=None, ge=1, le=50, strict=True)
    min_speakers: int | None = Field(default=None, ge=1, le=50, strict=True)
    max_speakers: int | None = Field(default=None, ge=1, le=50, strict=True)

    @model_validator(mode='after')
    def valid_options(self):
        if self.device not in {'auto', 'cuda', 'cpu'}:
            raise ValueError('设备请选择自动、GPU 或 CPU')
        if self.num_speakers is not None and (self.min_speakers is not None or self.max_speakers is not None):
            raise ValueError('固定人数和人数范围不能同时设置')
        if self.min_speakers and self.max_speakers and self.min_speakers > self.max_speakers:
            raise ValueError('最少人数不能大于最多人数')
        return self


def python_path(settings):
    return resolve(settings.diarization_python) if settings.diarization_python else ROOT / 'runtime/diarization-env' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def readiness(settings):
    model = resolve(settings.diarization_model)
    missing = [name for name in MODEL_FILES if not (model / name).is_file() or (model / name).stat().st_size < (1024 if name.endswith('.bin') else 1)]
    return dict(runtime_ready=python_path(settings).is_file(), model_ready=not missing,
                python=str(python_path(settings)), model=str(model), missing=missing,
                device=settings.diarization_device, ready=python_path(settings).is_file() and not missing)


def check_environment(settings):
    from .pipeline import offline_env
    info=readiness(settings)
    if not info['runtime_ready']:return {**info,'ok':False,'message':'独立 Python 尚未准备，请运行 prepare_diarization.py --runtime-only'}
    try:
        result=subprocess.run([python_path(settings),ROOT/'scripts/diarization_worker.py','--check'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60,env=offline_env(),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:raise ValueError('说话人识别依赖检查失败，请运行 prepare_diarization.py --runtime-only：'+result.stderr[-600:])
        value=json.loads(result.stdout.strip().splitlines()[-1])
        return {**info,**value,'ok':True,'message':'独立环境可用；模型文件已准备' if info['model_ready'] else '独立环境可用；请准备本地 Community-1 模型'}
    except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
        return {**info,'ok':False,'message':str(exc)}


def source_identity(folder):
    stat = (folder / 'source.media').stat()
    return [stat.st_size, stat.st_mtime_ns]


def validate_turns(turns, duration, *, exclusive=False):
    if not isinstance(turns, list) or len(turns) > 1000000:
        raise ValueError('说话人时间轴格式无效')
    result = []
    for turn in turns:
        start, end, speaker = turn.get('start'), turn.get('end'), turn.get('speaker')
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float))
                or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or start >= duration or end <= start or end > duration + .1
                or not isinstance(speaker, str) or not speaker or len(speaker) > 100):
            raise ValueError('说话人时间轴包含无效时间或标签')
        result.append(dict(start=float(start), end=min(float(end), duration), speaker=speaker))
    result.sort(key=lambda turn: (turn['start'], turn['end'], turn['speaker']))
    if exclusive and any(a['end'] > b['start'] + .001 for a, b in zip(result, result[1:])):
        raise ValueError('独占说话人时间轴包含重叠，未采用识别结果')
    if len({turn['speaker'] for turn in result}) > 50:
        raise ValueError('检测到超过 50 个说话人，请限制人数后重试')
    return result


def union_length(intervals):
    total = 0.; end = -1.
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end)); end = max(end, stop)
    return total


def overlap_windows(turns):
    """Intervals where different voices overlap; same-speaker tracks count once."""
    events = []
    for turn in turns:
        events += [(turn['start'], 1, turn['speaker']), (turn['end'], -1, turn['speaker'])]
    active = {}; previous = None; result = []
    for at, delta, speaker in sorted(events):
        if previous is not None and at > previous and len(active) > 1:result.append((previous, at))
        active[speaker] = active.get(speaker, 0) + delta
        if not active[speaker]:active.pop(speaker)
        previous = at
    return result


def match_segments(rows, exclusive, regular, label_map):
    overlap = overlap_windows(regular); result = []
    exclusive_ends=[turn['end'] for turn in exclusive]; overlap_ends=[end for start,end in overlap]
    for row in rows:
        start, end = row['start'], row['end']; length = end - start; weights = {}
        for index in range(bisect_right(exclusive_ends,start),len(exclusive)):
            turn=exclusive[index]
            if turn['start'] >= end:break
            seconds = max(0., min(end, turn['end']) - max(start, turn['start']))
            if seconds:weights[turn['speaker']] = weights.get(turn['speaker'], 0.) + seconds
        ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
        covered = sum(weights.values()); coverage = min(1., covered / length)
        agreement = ranked[0][1] / covered if covered else 0.
        intervals=[]
        for index in range(bisect_right(overlap_ends,start),len(overlap)):
            a,b=overlap[index]
            if a>=end:break
            intervals.append((max(start,a),min(end,b)))
        overlap_fraction = union_length(intervals) / length
        reasons = []
        if not ranked:reasons.append('未检测到人声')
        elif coverage < .4:reasons.append('人声覆盖较少')
        if ranked and agreement < .75:reasons.append('一句包含多个说话人')
        if overlap_fraction > .15:reasons.append('存在重叠对白')
        result.append(dict(id=row['id'], before=row['speaker'], after=label_map[ranked[0][0]] if ranked else None,
                           coverage=round(coverage, 3), agreement=round(agreement, 3),
                           overlap=round(overlap_fraction, 3), needs_review=bool(reasons), reasons=reasons))
    return result


def add_split_suggestions(rows, items, exclusive, words, label_map):
    from .segmentation import lexical

    turn_ends = [turn['end'] for turn in exclusive]
    centers = [((word['start'] + word['end']) / 2) for word in words]
    for index, (row, item) in enumerate(zip(rows, items)):
        start, end = row['start'], row['end']
        turns = []
        for turn_index in range(bisect_right(turn_ends, start),len(exclusive)):
            turn = exclusive[turn_index]
            if turn['start'] >= end:
                break
            if min(turn['end'], end) - max(turn['start'], start) >= .12:
                turns.append(turn)
        changes = [round(b['start'], 3) for a, b in zip(turns, turns[1:]) if a['speaker'] != b['speaker']]
        if not changes:
            continue
        item['split_times'] = changes
        item['needs_review'] = True
        reason = '一句包含多个说话人'
        if reason not in item['reasons']:
            item['reasons'].append(reason)
        if item['overlap'] > 0:
            continue

        selected = [word for word in words[bisect_right(centers, start - 1e-6):bisect_right(centers, end)] if lexical(word['word'])]
        if not selected or lexical(''.join(word['word'] for word in selected)) != lexical(row['text']):
            continue
        groups = []
        for word in selected:
            length = max(.001, word['end'] - word['start'])
            matches = [turn for turn in turns if min(word['end'],turn['end']) - max(word['start'],turn['start']) >= .7 * length]
            if len(matches) != 1:
                break
            speaker = label_map[matches[0]['speaker']]
            if not groups or groups[-1]['speaker'] != speaker:
                groups.append(dict(speaker=speaker, words=[]))
            groups[-1]['words'].append(word)
        else:
            if len(groups) - 1 != len(changes):
                continue
            offsets = [position for position, character in enumerate(row['text']) for _ in lexical(character)]
            boundaries = [0]
            times = [start]
            word_count = 0
            for before, after in zip(groups,groups[1:]):
                word_count += len(lexical(''.join(word['word'] for word in before['words'])))
                boundaries.append(offsets[word_count])
                times.append(round((before['words'][-1]['end'] + after['words'][0]['start']) / 2, 3))
            boundaries.append(len(row['text']))
            times.append(end)
            if any(b-a < .02 for a,b in zip(times,times[1:])) or (index+1 < len(rows) and times[-2] > rows[index+1]['start']):
                continue
            item['split_parts'] = [dict(text=row['text'][boundaries[i]:boundaries[i+1]].strip(),
                                        start=times[i],end=times[i+1],speaker=group['speaker'])
                                   for i,group in enumerate(groups)]


def cached_asr_words(folder, duration):
    from .pipeline import file_hash
    from .segmentation import flatten_words

    try:
        metadata = json.loads((folder/'cache/asr-words-metadata.json').read_text(encoding='utf-8'))
        if metadata.get('source') == file_hash(folder/'source.media'):
            raw = json.loads((folder/'cache/asr.json').read_text(encoding='utf-8'))
            return sorted(flatten_words(raw,duration),key=lambda word:(word['start']+word['end'])/2)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return []


def run_diarization(pipeline, identifier, settings, cancel):
    store = pipeline.store; project = store.read(identifier); folder = store.folder(identifier)
    request = project.get('diarization_request')
    if not request or request['revision'] != project.get('edit_revision', 0):
        raise ValueError('字幕已经更新，请重新发起说话人识别')
    ready = readiness(settings)
    if not ready['ready']:raise ValueError('说话人识别环境或模型尚未准备，请先在设置中检查 Community-1')
    source = source_identity(folder); options = DiarizationOptions.model_validate(request['options'])
    tag = request['id']; request_path = folder / 'cache' / f'diarization-{tag}-request.json'
    output = folder / 'cache' / f'diarization-{tag}-result.json'
    progress = folder / 'cache' / f'diarization-{tag}-progress.json'
    audio = folder / 'cache' / f'diarization-{tag}-16k.wav'
    prepared = []
    def report(value, message, detail=None):pipeline.progress(identifier, 'diarize', value, message, detail)
    try:
        report(0, '准备本地说话人识别音频', dict(phase='diarization_audio'))
        extract_audio(folder / 'source.media', audio, settings, folder / 'process.log', cancel, sample_rate=16000,
                      progress_duration=project['media']['duration'], on_progress=lambda done,total:report(5*done/total, '正在准备 16kHz 单声道音频', dict(phase='diarization_audio', done=done,total=total,unit='seconds')))
        selected_device=options.device
        if selected_device=='auto':
            environment=check_environment(settings)
            if not environment['ok']:raise ValueError(environment['message'])
            selected_device='cuda' if environment['cuda_available'] else 'cpu'
        if selected_device=='cuda':pipeline.tts.release('为本地说话人识别释放显存')
        atomic_json(request_path, dict(model=str(resolve(settings.diarization_model)), audio=str(audio),
                                      output=str(output), progress=str(progress), **{**options.model_dump(exclude_none=True),'device':selected_device}))
        last = None
        def tick():
            nonlocal last
            if not progress.is_file():return
            try:data = json.loads(progress.read_text(encoding='utf-8'))
            except (ValueError,OSError):return
            if data == last:return
            last = data
            report(10, data.get('message', '正在识别说话人'), {k:v for k,v in data.items() if k in {'phase','message','done','total','unit','current'}})
        from .pipeline import offline_env
        env = offline_env(); env.update(PYANNOTE_METRICS_ENABLED='0', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
        for key in ['HF_TOKEN','HUGGING_FACE_HUB_TOKEN']:env.pop(key,None)
        run([python_path(settings), ROOT/'scripts/diarization_worker.py', request_path], folder/'process.log', cancel, env=env, timeout=86400, on_tick=tick)
        check_cancel(cancel); data = json.loads(output.read_text(encoding='utf-8'))
        duration = project['media']['duration']
        regular = validate_turns(data['turns'], duration)
        exclusive = validate_turns(data['exclusive'], duration, exclusive=True)
        labels = list(dict.fromkeys(turn['speaker'] for turn in regular))
        if not labels or not exclusive:raise ValueError('未检测到可分配的人声，请检查原声或缩小识别人数范围')
        if set(turn['speaker'] for turn in exclusive) - set(labels):raise ValueError('说话人时间轴标签不一致')
        reused=reused_roles(project,exclusive,labels)
        label_map = {label:reused.get(label,f'diar_{tag[:8]}_{i+1}') for i,label in enumerate(labels)}
        roles={}
        for label in labels:
            speaker=label_map[label]
            name=role_name(project,speaker) if label in reused else unique_role_name(project,reserved=[r['name'] for r in roles.values()])
            roles[speaker]=dict(name=name,label=label,origin='diarization',reused=label in reused)
        items = match_segments(project['segments'], exclusive, regular, label_map)
        add_split_suggestions(project['segments'], items, exclusive, cached_asr_words(folder,duration), label_map)
        references = {}; origins = {}
        reference_rows = [dict(id=i+1, **turn) for i,turn in enumerate(regular)]
        for index,label in enumerate(labels):
            check_cancel(cancel); speaker=label_map[label]
            try:window = reference_window({'segments':reference_rows},label)
            except ValueError:continue
            name=f'diarization-ref-{tag}-{index+1}.wav';path=folder/name;prepared.append(path)
            report(90, f'为说话人 {index+1} 准备原片参考音', dict(phase='diarization_references',done=index,total=len(labels),unit='segments'))
            extract_audio(folder/'source.media',path,settings,folder/'process.log',cancel,start=window['start'],duration=window['duration'])
            references[speaker]=name;origins[speaker]=dict(kind='diarization',**window)
        check_cancel(cancel)
        with pipeline.lock:
            current = store.read(identifier)
            if current.get('edit_revision',0)!=request['revision'] or source_identity(folder)!=source:
                raise ValueError('识别期间字幕或原视频发生变化，当前分配未被覆盖，请重新识别')
            store.update(identifier,diarization_proposal=dict(request_id=tag,revision=request['revision'],status='ready',
                         source=source,roles=roles,references=references,reference_origins=origins,items=items,
                         turns=regular,device=data['device'],options=options.model_dump(exclude_none=True)))
        prepared=[]
        report(100, f'已识别 {len(roles)} 个说话人，请预览并采用字幕分配',dict(phase='diarization_review',done=len(items),total=len(items),unit='segments'))
    finally:
        audio.unlink(missing_ok=True)
        for path in prepared:path.unlink(missing_ok=True)


def allowed_reference_files(project):
    import re
    proposal = project.get('diarization_proposal') or {}
    if proposal.get('status') not in {'ready','applied'}:return set()
    return {name for name in proposal.get('references',{}).values() if re.fullmatch(r'diarization-ref-[0-9a-f]{24}-[1-9][0-9]?\.wav', name)}
