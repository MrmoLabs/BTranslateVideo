import io,json,threading,wave
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.server import create_app
from app.store import Store
from app.config import Settings
from app.generated_speech import resolve_speech,wav_info
from app.pipeline import make_voice_clip,tts_model_signature,dub_signature


def pcm(path,samples,rate=24000):
    import array
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as output:
        output.setparams((1,2,rate,0,'NONE',''));output.writeframes(array.array('h',samples).tobytes())


def fixture(tmp_path):
    store=Store(tmp_path/'projects');identifier=store.create('speech','en','course')['id'];folder=store.folder(identifier);(folder/'cache').mkdir()
    rows=[dict(id=1,start=0,end=1,text='First',zh='第一句。',speaker='speaker_1'),dict(id=2,start=2,end=3,text='Second',zh='第二句。',speaker='speaker_1')]
    store.update(identifier,segments=rows,media={'duration':4},roles={'speaker_1':{'name':'Narrator'}})
    settings=Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9999/v1')
    return store,identifier,folder,rows,settings


def raw(folder,row,settings,value=700):
    clip=make_voice_clip(row,folder,'',settings,tts_model_signature(settings),'','zh');path=Path(clip['output']);pcm(path,[value]*12000);return path


def test_full_track_plays_correct_aligned_samples_not_subtitle_window(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path)
    pcm(folder/'chinese.wav',[0]*12000+[300]*12000+[0]*30000+[900]*12000+[0]*30000)
    signature=dub_signature(store.read(identifier),folder,settings)
    store.update(identifier,outputs={'audio':'chinese.wav'},dub_signature=signature,dub_timing=[dict(id=1,start=.5,end=1),dict(id=2,start=2.25,end=2.75)])
    before=(folder/'project.json').read_bytes()
    result=resolve_speech(store,identifier,rows[1],settings)
    assert result['kind']=='timeline' and result['settings_current']
    path=folder/'cache'/('generated-cue-'+result['token']+'.wav')
    with wave.open(str(path),'rb') as stream:
        import array
        values=array.array('h',stream.readframes(stream.getnframes()))
    assert set(values)=={900} and len(values)==12000
    assert result['duration']==.5 and result['start']==2.25
    stamp=path.stat().st_mtime_ns
    assert resolve_speech(store,identifier,rows[1],settings)==result and path.stat().st_mtime_ns==stamp
    assert (folder/'project.json').read_bytes()==before


def test_completed_raw_cache_available_without_full_merge_and_no_tts(tmp_path,monkeypatch):
    store,identifier,folder,rows,settings=fixture(tmp_path);path=raw(folder,rows[0],settings)
    monkeypatch.setattr('app.pipeline.Pipeline.synthesize',lambda *args:pytest.fail('Playback must not call TTS'))
    before=path.read_bytes();result=resolve_speech(store,identifier,rows[0],settings,busy=True)
    assert result['kind']=='cache' and result['available'] and result['settings_current']
    assert path.read_bytes()==before


def test_edited_draft_prefers_its_cached_voice_and_preserves_saved_rows(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path);raw(folder,rows[0],settings)
    draft={**rows[0],'zh':'未保存的新稿。','emotion':'inherit'};raw(folder,draft,settings,1200)
    result=resolve_speech(store,identifier,draft,settings)
    assert result['segment']['zh']==draft['zh']
    assert store.read(identifier)['segments'][0]['zh']==rows[0]['zh']
    # No cache for the new draft: keep the old voice available with its old text.
    result=resolve_speech(store,identifier,{**draft,'zh':'另一个新稿。'},settings)
    assert result['available'] and result['segment']['zh']==rows[0]['zh']


def test_missing_and_corrupt_caches_do_not_generate_or_quarantine(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path)
    assert resolve_speech(store,identifier,rows[0],settings)=={'available':False}
    path=raw(folder,rows[0],settings);path.write_bytes(b'broken wave')
    assert resolve_speech(store,identifier,rows[0],settings)=={'available':False}
    assert path.read_bytes()==b'broken wave' and not list(folder.rglob('*.bak'))


def test_old_single_audition_and_changed_settings_are_explicit(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path)
    name='audition-'+'a'*24+'.wav';pcm(folder/name,[300]*12000)
    store.update(identifier,audition={'file':name,'segment':rows[0]})
    result=resolve_speech(store,identifier,rows[0],settings)
    assert result['kind']=='audition' and not result['settings_current']


def test_legacy_whole_track_window_and_missing_alignment(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path);pcm(folder/'chinese.wav',[300]*96000)
    store.update(identifier,outputs={'audio':'chinese.wav'})
    result=resolve_speech(store,identifier,rows[1],settings)
    assert result['kind']=='timeline_window' and result['start']==2 and result['end']==3


def test_no_raw_cache_reuse_after_model_voice_or_reference_change(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path);raw(folder,rows[0],settings)
    assert not resolve_speech(store,identifier,rows[0],settings.model_copy(update={'tts_voice':'different'}))['available']


def test_api_playback_is_revision_guarded_and_scoped(tmp_path,monkeypatch):
    settings=Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9999/v1');monkeypatch.setattr('app.config.load_settings',lambda:settings)
    app=create_app(tmp_path/'projects');store=app.state.store;p=store.create('api','en','course');identifier=p['id'];folder=store.folder(identifier)
    row=dict(id=1,start=0,end=1,text='Test',zh='测试。',speaker='speaker_1');store.update(identifier,segments=[row],media={'duration':2})
    settings=Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9999/v1');raw(folder,row,settings)
    monkeypatch.setattr('app.config.load_settings',lambda:settings)
    with TestClient(app) as client:
        body={'segment':row,'revision':1};response=client.post(f'/api/projects/{identifier}/generated-speech',json=body)
        assert response.status_code==200,response.text
        result=response.json();assert result['available']
        audio=client.get(result['url']);assert audio.status_code==200 and audio.content.startswith(b'RIFF')
        assert client.post(f'/api/projects/{identifier}/generated-speech',json={**body,'revision':0}).status_code==409
        assert client.get(f'/api/projects/{identifier}/generated-speech/chinese.wav').status_code==404
        assert not app.state.pipeline.tts.status()['alive']


def test_saved_text_or_voice_edits_can_still_play_explicit_old_index(tmp_path):
    from app.generated_speech import remember_clips
    store,identifier,folder,rows,settings=fixture(tmp_path);path=raw(folder,rows[0],settings)
    remember_clips(store,identifier,[rows[0]],[path],'zh')
    edited={**rows[0],'zh':'保存后的新台词。'};store.update(identifier,segments=[edited,rows[1]])
    result=resolve_speech(store,identifier,edited,settings.model_copy(update={'tts_voice':'new-voice'}))
    assert result['kind']=='previous_cache' and result['segment']['zh']==rows[0]['zh']
    assert not result['settings_current']


def test_legacy_previous_subtitles_locate_completed_old_cache(tmp_path):
    from app.config import atomic_json
    store,identifier,folder,rows,settings=fixture(tmp_path);raw(folder,rows[0],settings)
    atomic_json(folder/'segments.previous.json',rows)
    edited={**rows[0],'zh':'保存后的新稿。'};store.update(identifier,segments=[edited,rows[1]])
    result=resolve_speech(store,identifier,edited,settings)
    assert result['available'] and result['segment']['zh']==rows[0]['zh']


def test_legacy_window_prefers_complete_raw_voice_when_available(tmp_path):
    store,identifier,folder,rows,settings=fixture(tmp_path);raw(folder,rows[0],settings)
    pcm(folder/'chinese.wav',[300]*96000);store.update(identifier,outputs={'audio':'chinese.wav'})
    assert resolve_speech(store,identifier,rows[0],settings)['kind']=='cache'
