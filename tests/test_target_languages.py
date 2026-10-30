import importlib.util
import json
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.export_media import export_project
from app.languages import TARGET_LANGUAGES,target_code,target_files,tts_language
from app.media import ffmpeg,probe
from app.pipeline import Pipeline,make_voice_clip
from app.server import create_app
from app.translation import translate_batch
from app.translation_quality import speech_seconds


def row(**extra):
    return dict(id=1,start=0,end=2,text='Hello 12?',zh='你好12？',speaker='speaker_1',emotion='happy',**extra)


def test_target_import_defaults_validation_and_legacy_read(tmp_path,monkeypatch):
    source=tmp_path/'input.mp4';source.write_bytes(b'fixture')
    monkeypatch.setattr('app.server.probe',lambda *_:dict(duration=5,video=True,audio=True))
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        assert client.get('/api/target-languages').json()==TARGET_LANGUAGES
        default=client.post('/api/projects/import',json={'path':str(source)}).json()
        assert default['target_language']=='zh'
        for language in TARGET_LANGUAGES:
            response=client.post('/api/projects/import',json={'path':str(source),'target_language':language})
            assert response.status_code==200 and response.json()['target_language']==language
        assert client.post('/api/projects/import',json={'path':str(source),'target_language':'ko'}).status_code==400
        assert target_code({})=='zh' and target_files({})['audio']=='chinese.wav'


def test_switch_target_is_revision_checked_and_keeps_a_full_snapshot(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;project=store.create('test','en','course');id=project['id'];folder=store.folder(id)
        (folder/'cache').mkdir()
        for name in ['chinese.wav','chinese.srt','chinese.vtt','result.mkv','reference.wav','audition-'+'a'*24+'.wav']:(folder/name).write_bytes(name.encode())
        before=store.update(id,segments=[row()],media={'duration':3},references={'speaker_1':'reference.wav'},outputs={'audio':'chinese.wav','video':'result.mkv'},dub_signature='old',audition={'file':'audition-'+'a'*24+'.wav'},translation_proposal={'id':'old'})
        url=f'/api/projects/{id}/target-language';payload={'target_language':'ja','revision':before['edit_revision']}
        assert client.put(url,json={**payload,'revision':-1}).status_code==409
        app.state.pipeline.events[id]=threading.Event()
        try:assert client.put(url,json=payload).status_code==409
        finally:app.state.pipeline.events.pop(id)
        response=client.put(url,json=payload);assert response.status_code==200
        after=response.json();assert after['target_language']=='ja' and after['segments'][0]['zh']==''
        assert after['segments'][0]['text']==before['segments'][0]['text'] and after['segments'][0]['emotion']=='happy'
        assert after['references']==before['references'] and after['outputs']=={} and after['audition'] is None
        snapshot=next((folder/'history').iterdir())
        assert json.loads((snapshot/'project.json').read_text(encoding='utf-8'))['segments']==before['segments']
        assert (snapshot/before['audition']['file']).is_file() and (snapshot/'chinese.wav').is_file()
        assert (folder/'translated.vtt').read_text(encoding='utf-8').startswith('WEBVTT')
        assert client.get(f'/api/projects/{id}/files/chinese.srt').status_code==404
        assert client.put(url,json={'target_language':'ja','revision':after['edit_revision']}).status_code==200
        assert len(list((folder/'history').iterdir()))==1
        assert client.put(url,json=payload).status_code==409


def test_voice_cache_separates_target_languages_and_keeps_default_chinese(tmp_path):
    settings=Settings();args=(row(),tmp_path,'ref.wav',settings,[], 'reference hash')
    legacy=make_voice_clip(*args)
    chinese=make_voice_clip(*args,target_language='zh')
    english=make_voice_clip(*args,target_language='en')
    japanese=make_voice_clip(*args,target_language='ja')
    assert legacy==chinese and 'lang' not in legacy
    assert len({clip['output'] for clip in [chinese,english,japanese]})==3
    assert english['lang']=='EN' and japanese['lang']=='JA'
    for language in TARGET_LANGUAGES:assert tts_language(language)==language.upper()
    with pytest.raises(ValueError,match='2.5'):tts_language('ja','2')


def test_translation_target_controls_prompt_and_prevents_wrong_language_cache(tmp_path,monkeypatch):
    calls=[]
    async def post(url,payload,cancel):
        calls.append(payload)
        text='Hola 12?' if len(calls)==1 else 'こんにちは12？'
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'translations':[{'id':1,'translation':text}]},ensure_ascii=False)}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.translation._post_cancellable',post)
    settings=Settings(translation_review='off',translation_compact=False)
    first=translate_batch('http://127.0.0.1',settings,[row()],[],'course',threading.Event(),cache=tmp_path,topic={'target_language':'es'})
    again=translate_batch('http://127.0.0.1',settings,[row()],[],'course',threading.Event(),cache=tmp_path,topic={'target_language':'es'})
    other=translate_batch('http://127.0.0.1',settings,[row()],[],'course',threading.Event(),cache=tmp_path,topic={'target_language':'ja'})
    assert first==again=={1:'Hola 12?'} and other=={1:'こんにちは12？'} and len(calls)==2
    assert '西班牙语' in calls[0]['messages'][0]['content'] and '日语' in calls[1]['messages'][0]['content']
    assert '始终输出简体中文' not in calls[0]['messages'][0]['content']
    assert speech_seconds('これは日本語です。','ja')>0 and speech_seconds('هذه جملة عربية','ar')>0


@pytest.mark.parametrize('language',['EN','JA','ES','AR'])
def test_non_chinese_uses_correct_native_language_without_chinese_batching(tmp_path,monkeypatch,language):
    spec=importlib.util.spec_from_file_location('target_batch',Path(__file__).parents[1]/'scripts/tts_batch.py');batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(batch)
    cuda=SimpleNamespace(mem_get_info=lambda:(8*1024**3,16*1024**3),memory_reserved=lambda:0,memory_allocated=lambda:0,synchronize=lambda:None,max_memory_allocated=lambda:0)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=cuda))
    calls=[]
    def infer(**kwargs):calls.append(kwargs);Path(kwargs['output_path']).write_bytes(b'voice')
    model=SimpleNamespace(cache_spk_audio_prompt='reference',cache_spk_cond=True,infer=infer)
    monkeypatch.setattr(batch,'native_batch',lambda *_:(_ for _ in ()).throw(AssertionError('Chinese batching must not be used')))
    clips=[dict(id=i,text='sample',reference='reference',output=str(tmp_path/f'{i}.wav'),lang=language) for i in range(2)]
    stats=batch.run_clips(model,clips,4)
    assert len(calls)==2 and all(call['lang']==language for call in calls) and stats['max_actual_batch']==1


def test_real_non_chinese_export_has_correct_track_tags_and_target_caption_files(tmp_path):
    app=create_app(tmp_path/'projects');store=app.state.store
    project=store.create('Spanish export','en','course',target_language='es');id=project['id'];folder=store.folder(id);(folder/'cache').mkdir()
    settings=Settings(preview_encoder='cpu');cancel=threading.Event()
    ffmpeg(settings,['-f','lavfi','-i','color=c=green:s=160x90:r=15:d=2','-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-c:a','aac','-shortest','-f','matroska',str(folder/'source.media')],folder/'process.log',cancel)
    with wave.open(str(folder/'translated.wav'),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(24000);stream.writeframes(b'\0\0'*48000)
    store.update(id,segments=[{**row(),'zh':'Hola 12?'}],media={'duration':2},outputs={'audio':'translated.wav'},dub_signature='test',background_mode='none')
    try:
        app.state.pipeline.write_subtitles(id)
        export_project(store,id,settings,cancel,lambda *_:None,{})
        data=store.read(id);info=probe(folder/'result.mkv',settings)
        audio=[s for s in info['streams'] if s['codec_type']=='audio']
        assert [s.get('tags',{}).get('language') for s in audio]==['eng','spa']
        assert next(s for s in info['streams'] if s['codec_type']=='subtitle')['tags']['language']=='spa'
        assert data['outputs']['audio']=='translated.wav' and data['outputs']['target_subtitles']=='translated.srt'
        preview=probe(folder/data['outputs']['preview'],settings)
        assert next(s for s in preview['streams'] if s['codec_type']=='audio')['tags']['language']=='spa'
    finally:app.state.pipeline.close()


def test_different_target_languages_are_never_combined_in_one_native_batch(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('mixed_target_batch',Path(__file__).parents[1]/'scripts/tts_batch.py');batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(batch)
    cuda=SimpleNamespace(mem_get_info=lambda:(8*1024**3,16*1024**3),memory_reserved=lambda:0,memory_allocated=lambda:0,synchronize=lambda:None,max_memory_allocated=lambda:0)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=cuda));native=[];singles=[]
    def infer(**kwargs):singles.append(kwargs['lang']);Path(kwargs['output_path']).write_bytes(b'voice')
    def grouped(model,clips):
        native.append([clip['lang'] for clip in clips])
        for clip in clips:Path(clip['output']).write_bytes(b'voice')
    monkeypatch.setattr(batch,'native_batch',grouped)
    model=SimpleNamespace(cache_spk_audio_prompt='reference',cache_spk_cond=True,infer=infer)
    clips=[dict(id=i,text='sample',reference='reference',output=str(tmp_path/f'{i}.wav'),lang=lang) for i,lang in enumerate(['ZH','ZH','EN','EN'])]
    batch.run_clips(model,clips,4)
    assert native==[['ZH','ZH']] and singles==['EN','EN']
