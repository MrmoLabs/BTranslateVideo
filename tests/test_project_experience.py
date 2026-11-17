from contextlib import contextmanager
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from app.config import Settings, atomic_json
from app.pipeline import Pipeline, file_hash
from app.project_experience import asr_profile, effective_asr, summary
from app.server import create_app
from app.store import Store


def row():
    return dict(id=1,start=.18,end=.54,text='哥哥。',zh='',speaker='speaker_1',words=[dict(word='哥哥。',start=.18,end=.54)])


def test_goal_summary_uses_outputs_instead_of_last_task_status():
    project=dict(status='done',segments=[row()],outputs={})
    assert summary(project)==dict(goal='dub',code='source_ready',source_count=1,translation_count=0)
    assert summary({**project,'goal':'transcribe'})['goal']=='transcribe'
    assert summary({**project,'outputs':{'video':'result.mkv'}})['code']=='video_ready'


def test_goal_and_asr_changes_preserve_subtitles_and_reject_stale_edits(tmp_path,monkeypatch):
    monkeypatch.setattr('app.project_experience.load_settings',lambda:Settings())
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;p=store.create('experience','zh','course');pid=p['id']
        original=store.update(pid,segments=[row()],media={'duration':2},outputs={'video':'keep.mkv'})
        result=client.put(f'/api/projects/{pid}/goal',json=dict(goal='transcribe',revision=original['edit_revision']))
        assert result.status_code==200,result.text
        current=result.json();assert current['segments']==original['segments'] and current['outputs']==original['outputs']
        assert current['workflow']['code']=='video_ready'
        value=client.put(f'/api/projects/{pid}/asr-options',json=dict(provider='fun_asr',language='zh',device='cpu',hotwords='重庆',revision=current['edit_revision']))
        assert value.status_code==200,value.text
        after=value.json();assert after['asr_next']['provider']=='fun_asr' and after['asr_next']['hotwords']=='重庆'
        assert after['segments']==original['segments'] and after['outputs']==original['outputs']
        assert client.put(f'/api/projects/{pid}/goal',json=dict(goal='dub',revision=current['edit_revision'])).status_code==409
        assert store.list()[0]['goal']=='transcribe'
        bad=client.put(f'/api/projects/{pid}/asr-options',json=dict(provider='fun_asr',language='ko',revision=after['edit_revision']))
        assert bad.status_code==400 and store.read(pid)['language']=='zh'


def test_incompatible_language_fails_before_probe_copy_or_project_creation(tmp_path,monkeypatch):
    monkeypatch.setattr('app.server.load_settings',lambda:Settings(asr_provider='fun_asr'))
    monkeypatch.setattr('app.server.probe',lambda *a:pytest.fail('must reject before probing or copying'))
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        result=client.post('/api/projects/import',json=dict(path=str(tmp_path/'missing.mp4'),language='ko',goal='transcribe'))
        assert result.status_code==400 and not app.state.store.list()


@pytest.mark.parametrize('goal,expected',[('transcribe',['analyze']),('translate',['analyze','translate']),('dub',['analyze','translate','dub','export'])])
def test_all_runs_only_the_requested_goal_steps(tmp_path,monkeypatch,goal,expected):
    store=Store(tmp_path/'projects');p=store.create('goal','en','course',goal=goal);pid=p['id']
    pipeline=Pipeline(store);seen=[]
    for stage in ['analyze','translate','dub','export']:
        def work(identifier,settings,cancel,name=stage):
            seen.append(name)
            if name=='analyze':store.update(pid,segments=[row()])
            if name=='translate':store.update(pid,segments=[{**row(),'zh':'Hi.'}])
        monkeypatch.setattr(pipeline,stage,work)
    monkeypatch.setattr('app.pipeline.validate_tts',lambda *a:seen.append('tts-validation') if goal=='dub' else pytest.fail('No TTS for subtitle tasks'))
    try:
        pipeline.execute(pid,'all',Settings(),threading.Event())
        assert [s for s in seen if s!='tts-validation']==expected
        assert store.read(pid)['status']=='done'
    finally:pipeline.close()


def test_resegment_reuses_original_asr_when_current_backend_has_changed(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');p=store.create('cache','zh','course');pid=p['id'];folder=store.folder(pid)
    cache=folder/'cache';cache.mkdir();source=folder/'source.media';source.write_bytes(b'original source')
    store.update(pid,segments=[dict(id=1,start=.18,end=.54,text='哥哥。',zh='old',speaker='speaker_1')],media={'duration':2})
    atomic_json(cache/'asr.json',[row()]);atomic_json(cache/'asr-words-metadata.json',dict(source=file_hash(source),provider='faster_whisper',model='old-model'))
    monkeypatch.setattr('app.pipeline.transcribe',lambda *a,**kw:pytest.fail('Resegment must not invoke another ASR'))
    monkeypatch.setattr('app.pipeline.semantic_segments',lambda raw,*a,**kw:raw)
    @contextmanager
    def translator(*a):yield 'unused'
    monkeypatch.setattr('app.pipeline.local_translator',translator)
    pipeline=Pipeline(store);monkeypatch.setattr(pipeline,'audio',lambda *a:cache/'source.wav')
    try:
        pipeline.analyze(pid,Settings(asr_provider='fun_asr'),threading.Event(),force=True)
        assert store.read(pid)['segments'][0]['text']=='哥哥。'
        assert json.loads((cache/'asr-words-metadata.json').read_text())['model']=='old-model'
        assert list((folder/'history').iterdir())
    finally:pipeline.close()


def test_same_language_source_copy_is_explicit_and_revision_guarded(tmp_path,monkeypatch):
    monkeypatch.setattr('app.project_experience.load_settings',lambda:Settings())
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;pid=store.create('copy','zh','course')['id']
        p=store.update(pid,segments=[row()],media={'duration':2})
        result=client.post(f'/api/projects/{pid}/copy-source',json={'revision':p['edit_revision']})
        assert result.status_code==200,result.text
        assert result.json()['segments'][0]['zh']=='哥哥。' and result.json()['segments'][0]['text']=='哥哥。'
        assert client.post(f'/api/projects/{pid}/copy-source',json={'revision':p['edit_revision']}).status_code==409


def test_project_overrides_do_not_mutate_global_settings():
    global_settings=Settings()
    effective=effective_asr(global_settings,{'asr_options':dict(asr_provider='fun_asr',fun_asr_hotwords='四川',tts_provider='openai_speech')})
    assert effective.asr_provider=='fun_asr' and effective.tts_provider=='indextts'
    assert global_settings.asr_provider=='faster_whisper'
    assert asr_profile(effective,'zh')['supported_languages']==['','zh','en','ja']
