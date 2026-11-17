"""Revision-guarded local speaker analysis and explicit proposal adoption."""
from __future__ import annotations
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .config import load_settings, Settings
from .project_roles import project_roles
from .diarization import DiarizationOptions, readiness, source_identity, check_environment


class StartDiarization(DiarizationOptions):
    revision: int = Field(ge=0)


class Assignment(BaseModel):
    id: int
    speaker: str


class ApplyDiarization(BaseModel):
    revision: int = Field(ge=0)
    request_id: str
    assignments: list[Assignment] = Field(min_length=1, max_length=100000)


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
            eligible={item['id'] for item in proposal['items']}
            if len(choices)!=len(data.assignments) or set(choices)-eligible or set(choices.values())-(set(proposal['roles'])|project_roles(project)):
                raise ValueError('只能采用当前识别结果中的句子和角色，且句子不能重复')
            rows=[{**row,'speaker':choices[row['id']]} if row['id'] in choices else dict(row) for row in project['segments']]
            if not set(choices).issubset({row['id'] for row in rows}):raise HTTPException(409,'字幕编号已改变，请重新识别')
            pipeline.snapshot(identifier)
            used=set(choices.values());refs={key:value for key,value in proposal['references'].items() if key in used and not project['references'].get(key)}
            for name in refs.values():
                if not (store.folder(identifier)/name).is_file():raise ValueError('候选参考音文件已丢失，请重新识别')
            existing=project_roles(project)
            roles={**project.get('roles',{}),**{key:dict(name=value['name'],origin='diarization') for key,value in proposal['roles'].items() if key in used and key not in existing}}
            for speaker in used:
                if speaker in existing:roles[speaker]={**roles.get(speaker,{}),'diarization_linked':True}
            updated=store.update(identifier,segments=rows,roles=roles,references={**project['references'],**refs},
                reference_origins={**project.get('reference_origins',{}),**{key:value for key,value in proposal['reference_origins'].items() if key in refs}},
                diarization_proposal={**proposal,'status':'applied','applied_ids':sorted(choices)},outputs={},dub_signature='',export_signature='',
                audition=None,audition_request=None,dub_timing=[],preview_info={},tts_runtime=None,status='idle',error='')
            pipeline.write_subtitles(identifier)
            store.log(identifier,f'已采用 {len(choices)} 句说话人分配；原角色与音色保留，旧字幕和成片已备份')
            return updated
