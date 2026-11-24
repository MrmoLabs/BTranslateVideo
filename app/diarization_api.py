"""Revision-guarded local speaker analysis and explicit proposal adoption."""
from __future__ import annotations
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .config import load_settings, Settings
from .project_roles import project_roles
from .diarization import DiarizationOptions, readiness, source_identity, check_environment
from .subtitles import validate_segments


class StartDiarization(DiarizationOptions):
    revision: int = Field(ge=0)


class Assignment(BaseModel):
    id: int
    speaker: str


class SplitSelection(BaseModel):
    id: int
    retranslate: bool = False


class ApplyDiarization(BaseModel):
    revision: int = Field(ge=0)
    request_id: str
    assignments: list[Assignment] = Field(default_factory=list, max_length=100000)
    splits: list[SplitSelection] = Field(default_factory=list, max_length=30000)


def attach_diarization(app, store, pipeline, editable):
    @app.get('/api/diarization/environment')
    def environment():
        return readiness(load_settings())

    @app.post('/api/diarization/check')
    def check(data:dict):
        return check_environment(Settings.model_validate(data.get('settings',load_settings().model_dump())))

    @app.post('/api/projects/{identifier}/diarization')
    def start(identifier: str, data: StartDiarization):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier);settings=load_settings()
            if data.revision!=project.get('edit_revision',0):raise HTTPException(409,'字幕已更新，请保存并重新识别说话人')
            if not project['segments']:raise ValueError('请先识别原文或导入字幕')
            if not project.get('media',{}).get('audio'):raise ValueError('这个项目没有可识别的原片音轨')
            if not readiness(settings)['ready']:raise ValueError('说话人识别环境或模型尚未准备，请先在设置中检查 Community-1')
            request_id=uuid.uuid4().hex[:24]
            store.update(identifier,diarization_request=dict(id=request_id,revision=data.revision,
                         options=DiarizationOptions.model_validate(data.model_dump()).model_dump(exclude_none=True)))
            pipeline.submit(identifier,'diarize',settings)
            return dict(queued=True,request_id=request_id)

    @app.post('/api/projects/{identifier}/diarization/apply')
    def apply(identifier: str, data: ApplyDiarization):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier);proposal=project.get('diarization_proposal') or {}
            if data.revision!=project.get('edit_revision',0) or proposal.get('revision')!=data.revision:
                raise HTTPException(409,'字幕已修改，识别分配尚未采用，请重新识别')
            if proposal.get('status')!='ready' or proposal.get('request_id')!=data.request_id:
                raise HTTPException(409,'识别结果已经失效，请重新识别')
            if proposal['source']!=source_identity(store.folder(identifier)):raise HTTPException(409,'原视频已变更，请重新识别')
            choices={item.id:item.speaker for item in data.assignments}
            splits={item.id:item for item in data.splits}
            eligible={item['id']:item for item in proposal['items']}
            if len(choices)!=len(data.assignments) or choices.keys()-eligible.keys() or set(choices.values())-(set(proposal['roles'])|project_roles(project)):
                raise ValueError('只能采用当前识别结果中的句子和角色，且句子不能重复')
            selected=choices.keys()|splits.keys()
            if not selected or choices.keys()&splits.keys() or len(splits)!=len(data.splits) or any(not eligible.get(i,{}).get('split_parts') for i in splits):
                raise ValueError('请选择有效的角色分配或拆句建议，同一句只能采用一次')
            if not selected.issubset({row['id'] for row in project['segments']}):raise HTTPException(409,'字幕编号已改变，请重新识别')
            rows=[]
            used=set(choices.values())
            next_id=max(row['id'] for row in project['segments'])+1
            for row in project['segments']:
                row_id=row['id']
                if row_id not in splits:
                    rows.append({**row,'speaker':choices.get(row_id,row['speaker'])})
                    continue
                if row['zh'].strip() and not splits[row_id].retranslate:
                    raise ValueError('拆句会重置本句译文，请明确选择重新翻译')
                for index,part in enumerate(eligible[row_id]['split_parts']):
                    piece={key:value for key,value in row.items() if key not in {'words','review_status','review_signature'}}
                    rows.append({**piece,**part,'id':next_id if index else row_id,'zh':''})
                    next_id+=bool(index)
                    used.add(part['speaker'])
            if splits:
                validate_segments(rows,project['media']['duration'])
            split_checks={str(row_id) for row_id in splits}
            pipeline.snapshot(identifier)
            refs={key:value for key,value in proposal['references'].items() if key in used and not project['references'].get(key)}
            for name in refs.values():
                if not (store.folder(identifier)/name).is_file():raise ValueError('候选参考音文件已丢失，请重新识别')
            existing=project_roles(project)
            roles={**project.get('roles',{}),**{key:dict(name=value['name'],origin='diarization') for key,value in proposal['roles'].items() if key in used and key not in existing}}
            for speaker in used:
                if speaker in existing:roles[speaker]={**roles.get(speaker,{}),'diarization_linked':True}
            updated=store.update(identifier,segments=rows,roles=roles,references={**project['references'],**refs},
                reference_origins={**project.get('reference_origins',{}),**{key:value for key,value in proposal.get('reference_origins',{}).items() if key in refs}},
                diarization_proposal={**proposal,'status':'applied','applied_ids':sorted(selected)},outputs={},dub_signature='',export_signature='',
                translation_checks={key:value for key,value in project.get('translation_checks',{}).items() if key not in split_checks},
                audition=None,audition_request=None,dub_timing=[],preview_info={},tts_runtime=None,status='idle',error='')
            pipeline.write_subtitles(identifier)
            store.log(identifier,f'已采用 {len(choices)} 句角色分配和 {len(splits)} 处拆句，旧字幕和成片已备份')
            return updated
