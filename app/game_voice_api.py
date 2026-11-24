"""Local API for game voice editing, audition, and MOD exports."""
from threading import Event
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import drama3, voice_conversion
from .config import load_settings
from .game_voice import MAX_GUIDE_BYTES, GameVoiceProjects, audio_info, bandit_available, project_id, tools, validated_update
from .nier_audio import safe_file, sound_root


Language = Literal['ja', 'en']


class NewProject(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=2048)
    language: Language = 'ja'


class Revision(BaseModel):
    revision: int = Field(ge=0)


class LineEdit(Revision):
    ja: str = Field('', max_length=4000)
    en: str = Field('', max_length=4000)
    text: str = Field('', max_length=1500)
    role: str = Field('', max_length=80)
    scene: str = Field('', max_length=120)
    scene_order: int = Field(0, ge=0, le=99999)
    notes: str = Field('', max_length=1500)
    voice: str = Field('', max_length=32)
    emotion: str = 'inherit'
    emotion_strength: Literal['low', 'medium', 'high'] = 'medium'
    preserve_background: bool = False
    background_backend: Literal['demucs', 'bandit'] = 'demucs'
    speed: float = Field(1, ge=.7, le=1.5)
    gain: float = Field(0, ge=-18, le=12)
    trim_start: float = Field(0, ge=0, le=5)
    trim_end: float = Field(0, ge=0, le=5)
    lead: float = Field(0, ge=0, le=5)
    tail: float = Field(0, ge=0, le=5)
    target_duration: float = Field(0, ge=0, le=120)
    drama3_direction: str = Field('', max_length=300)
    drama3_reference_text: str = Field('', max_length=1500)


class ChooseCandidate(Revision):
    candidate: str = Field(pattern=r'^[a-f0-9]{24}$')


class TestResult(Revision):
    tested: bool


class RowSelection(Revision):
    rows: list[str] = Field(min_length=1, max_length=150)


class OriginalSelection(RowSelection):
    original: bool
    protect_adopted: bool = False


class PackagePreview(RowSelection):
    release: bool = False


class Job(RowSelection):
    action: Literal['asr', 'translate', 'polish', 'transcribe_translate', 'voice_from_source', 'voice_from_selection',
                    'analyze_fit', 'generate', 'alternate', 'package']
    language: Language = 'ja'
    voice: str = Field('', max_length=32)
    release: bool = False
    role: str = Field('', max_length=80)
    name: str = Field('', max_length=100)
    emotion: str = Field('', max_length=20)
    force_batch_settings: bool = False
    route: Literal['warm', 'project', 'direct', 'conversion', 'drama3', 'drama3_conversion'] = 'project'


class VoiceFromSource(Revision):
    name: str = Field('', max_length=100)


class AnalyzeFit(Revision):
    q: str = Field('', max_length=200)
    group: str = ''
    role: str = ''
    scene: str = ''
    status: str = ''
    limit: int = Field(0, ge=0, le=10000)


class PronunciationRule(BaseModel):
    term: str = Field(min_length=1, max_length=80)
    spoken: str = Field(min_length=1, max_length=160)


class Pronunciation(Revision):
    rules: list[PronunciationRule] = Field(max_length=100)


class RoleVoices(Revision):
    role_voices: dict[str, str] = Field(default_factory=dict)


class VoiceConversion(Revision):
    enabled: bool


def attach(app, store, pipeline, voices):
    projects = GameVoiceProjects(store.root.parent / 'game_projects', pipeline, voices)
    router = APIRouter(prefix='/api/game-voices')

    @router.get('/tools')
    def tool_status():
        decoder, encoder, template = tools()
        settings = load_settings()
        drama3_problem = drama3.status(settings)
        drama3_warm = not drama3_problem and pipeline.drama3.warm(settings)
        index_warm = settings.tts_provider == 'indextts' and pipeline.tts.status()['loaded']
        warm_model = 'Drama 3' if drama3_warm else 'IndexTTS' if index_warm else ''
        return dict(decoder=bool(decoder), encoder=bool(encoder), template=bool(template),
                    vc=voice_conversion.available(settings), bandit=bandit_available(),
                    drama3=not drama3_problem, drama3_status=drama3_problem,
                    drama3_warm=drama3_warm, warm_model=warm_model, drama3_idle_seconds=settings.drama3_idle_seconds)

    @router.get('')
    def list_projects():
        return projects.list()

    @router.post('')
    def create(data: NewProject):
        return projects.create(data.title, sound_root(data.path.strip()), data.language)

    @router.get('/{identifier}')
    def detail(identifier: str):
        return projects.detail(identifier)

    @router.post('/{identifier}/import-text')
    def import_text(identifier: str, payload: Revision, overwrite: bool = False):
        return projects.import_text(identifier, payload.revision, overwrite=overwrite)

    @router.get('/{identifier}/rows')
    def lines(identifier: str, q: str = Query('', max_length=200), group: str = '', role: str = '', scene: str = '',
              status: Literal['', 'replaceable', 'tested', 'ready', 'unvoiced', 'timing', 'no-script', 'no-translation',
                              'no-text', 'has-suggestion', 'skipped', 'risky', 'original', 'pending'] = '',
              offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        return projects.rows(identifier, q, group, role, scene, status, offset, limit)

    @router.get('/{identifier}/rows/{row_id}')
    def line(identifier: str, row_id: str):
        return dict(**projects.row(identifier, row_id), revision=projects.state(identifier)['revision'])

    @router.put('/{identifier}/rows/{row_id}')
    def save(identifier: str, row_id: str, payload: LineEdit):
        with pipeline.lock:
            state = projects.editable(identifier, payload.revision)
            row = projects.row(identifier, row_id, state)
            edit = validated_update(row, payload.model_dump(exclude={'revision'}))
            for key in {'scene_order', 'target_duration', 'background_backend',
                        'drama3_direction', 'drama3_reference_text'} - payload.model_fields_set:
                edit.pop(key)
            if edit['voice']:
                projects.voice(edit['voice'])
            revision = projects.save_rows(identifier, {row_id: edit})
        return dict(**projects.row(identifier, row_id), revision=revision)

    @router.post('/{identifier}/rows/{row_id}/adopt')
    def adopt(identifier: str, row_id: str, payload: ChooseCandidate):
        with pipeline.lock:
            state = projects.editable(identifier, payload.revision)
            row = projects.row(identifier, row_id, state)
            projects.candidate(identifier, row_id, payload.candidate)
            if row['selected'] != payload.candidate or row['keep_original']:
                tested = row['tested'] and row['selected'] == payload.candidate
                projects.save_rows(identifier, {row_id: {'selected': payload.candidate, 'tested': tested, 'keep_original': False}})
        return line(identifier, row_id)

    @router.put('/{identifier}/original-selection')
    def select_original(identifier: str, payload: OriginalSelection):
        return projects.set_original(identifier, payload.rows, payload.original, payload.revision,
                                     protect_adopted=payload.protect_adopted)

    @router.post('/{identifier}/rows/{row_id}/defer')
    def defer(identifier: str, row_id: str, payload: Revision):
        with pipeline.lock:
            row = projects.row(identifier, row_id, projects.editable(identifier, payload.revision))
            if row['decision'] != 'pending':
                projects.save_rows(identifier, {row_id: {'keep_original': False, 'selected': '', 'tested': False}})
        return line(identifier, row_id)

    @router.put('/{identifier}/rows/{row_id}/guide')
    def upload_guide(identifier: str, row_id: str, revision: int = Form(ge=0), audio: UploadFile = File(...)):
        projects.upload_guide(identifier, row_id, audio.file.read(MAX_GUIDE_BYTES + 1), audio.filename or 'WAV',
                              revision, load_settings())
        return line(identifier, row_id)

    @router.get('/{identifier}/rows/{row_id}/guide')
    def guide_audio(identifier: str, row_id: str):
        return FileResponse(projects.guide(identifier, row_id), media_type='audio/wav')

    @router.delete('/{identifier}/rows/{row_id}/guide')
    def remove_guide(identifier: str, row_id: str, payload: Revision):
        with pipeline.lock:
            row = projects.row(identifier, row_id, projects.editable(identifier, payload.revision))
            if row.get('guide'):
                projects.save_rows(identifier, {row_id: {'guide': None}})
        return line(identifier, row_id)

    @router.post('/{identifier}/package-preview')
    def preview_package(identifier: str, payload: PackagePreview):
        with pipeline.lock:
            state = projects.editable(identifier, payload.revision)
            rows = [projects.row(identifier, row_id, state) for row_id in dict.fromkeys(payload.rows)]
            return dict(rows=projects.package_plan(identifier, rows, payload.release))

    @router.post('/{identifier}/rows/{row_id}/test-result')
    def test_result(identifier: str, row_id: str, payload: TestResult):
        with pipeline.lock:
            state = projects.editable(identifier, payload.revision)
            row = projects.row(identifier, row_id, state)
            if row['decision'] != 'chinese' or not row['replaceable']:
                raise ValueError('请先采用一条可替换语音')
            if payload.tested and not any(package.get('clips', {}).get(row_id) == row['selected'] for package in state['packages']):
                raise ValueError('请先把当前候选生成测试 MOD，再进游戏验收')
            projects.save_rows(identifier, {row_id: {'tested': payload.tested}})
        return line(identifier, row_id)

    def source_path(identifier, row_id, language):
        try:
            return projects.source(identifier, row_id, language, load_settings(), Event())
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/{identifier}/rows/{row_id}/source/{language}')
    def source_audio(identifier: str, row_id: str, language: Language, native: bool = False):
        if native and projects.row(identifier, row_id)['channels'] in {2, 6}:
            return FileResponse(projects.full_source(identifier, row_id, language, load_settings(), Event()), media_type='audio/wav')
        return FileResponse(source_path(identifier, row_id, language), media_type='audio/wav')

    @router.get('/{identifier}/rows/{row_id}/source/{language}/waveform')
    def source_waveform(identifier: str, row_id: str, language: Language):
        return audio_info(source_path(identifier, row_id, language))

    @router.get('/{identifier}/rows/{row_id}/candidates/{candidate}')
    def candidate_audio(identifier: str, row_id: str, candidate: str,
                        track: Literal['preview', 'dry', 'mix', 'background', 'performance'] = 'preview'):
        return FileResponse(projects.candidate(identifier, row_id, candidate, track), media_type='audio/wav')

    @router.delete('/{identifier}/rows/{row_id}/candidates/{candidate}')
    def delete_candidate(identifier: str, row_id: str, candidate: str, payload: Revision):
        with pipeline.lock:
            state = projects.editable(identifier, payload.revision)
            row = projects.row(identifier, row_id, state)
            candidates = row['candidates'].copy()
            if candidates.pop(candidate, None) is None:
                raise FileNotFoundError(candidate)
            update = {'candidates': candidates}
            if row['selected'] == candidate:
                update.update(selected='', tested=False)
            revision = projects.save_rows(identifier, {row_id: update})
            return dict(**projects.row(identifier, row_id), revision=revision)

    @router.get('/{identifier}/rows/{row_id}/candidates/{candidate}/waveform')
    def candidate_waveform(identifier: str, row_id: str, candidate: str,
                           track: Literal['preview', 'dry', 'mix', 'background', 'performance'] = 'preview'):
        return audio_info(projects.candidate(identifier, row_id, candidate, track))

    @router.post('/{identifier}/analyze-fit')
    def analyze_fit(identifier: str, payload: AnalyzeFit):
        row_ids = projects.fit_row_ids(identifier, payload.q, payload.group, payload.role,
                                       payload.scene, payload.status, payload.limit)
        if not row_ids:
            raise ValueError('当前筛选没有可分析的台词')
        return projects.submit(identifier, 'analyze_fit', row_ids, load_settings(), payload.revision)

    @router.post('/{identifier}/jobs')
    def submit(identifier: str, payload: Job):
        return projects.submit(identifier, payload.action, payload.rows, load_settings(), payload.revision,
            language=payload.language, voice=payload.voice, release=payload.release,
            role=payload.role, name=payload.name, emotion=payload.emotion,
            force_batch_settings=payload.force_batch_settings, route=payload.route)

    @router.post('/{identifier}/roles/{role}/voice-from-source')
    def voice_from_source(identifier: str, role: str, payload: VoiceFromSource):
        data = projects.state(identifier)
        language = data.get('language', 'ja')
        row_ids = projects.voice_source_rows(identifier, role, language)
        if not row_ids:
            raise ValueError('该角色没有可用的原声台词')
        return projects.submit(identifier, 'voice_from_source', row_ids, load_settings(), payload.revision,
            language=language, role=role, name=payload.name)

    @router.post('/{identifier}/cancel')
    def cancel(identifier: str):
        return projects.cancel(identifier)

    @router.post('/{identifier}/retry')
    def retry(identifier: str, payload: Revision):
        return projects.retry(identifier, load_settings(), payload.revision)

    @router.put('/{identifier}/pronunciation')
    def pronunciation(identifier: str, payload: Pronunciation):
        return projects.save_pronunciation(identifier, [rule.model_dump() for rule in payload.rules], payload.revision)

    @router.put('/{identifier}/role-voices')
    def role_voices(identifier: str, payload: RoleVoices):
        return projects.save_role_voices(identifier, payload.role_voices, payload.revision)

    @router.put('/{identifier}/voice-conversion')
    def voice_conversion_toggle(identifier: str, payload: VoiceConversion):
        return projects.set_voice_conversion(identifier, payload.enabled, payload.revision)

    @router.get('/{identifier}/packages/{package}')
    def download(identifier: str, package: str):
        data = projects.state(identifier)
        project_id(package)
        item = next((value for value in data['packages'] if value['id'] == package), None)
        if item is None:
            raise FileNotFoundError(package)
        tag = 'release' if item['release'] else 'test'
        path = safe_file(projects.folder(identifier), f'packages/{package}.zip')
        return FileResponse(path, filename=f'NieRAutomata-ChineseVoice-{tag}-{package[:8]}.zip', media_type='application/zip')

    app.include_router(router)
    return projects
