import asyncio
import copy
import io
import json
import threading
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.media import Cancelled, wav_duration
from app.model_backends import (catalog, chat_payload, check_backend, parse_transcription,
    synthesize_service, transcribe, tts_model_signature, validate_tts)
from app.pipeline import Pipeline, make_voice_clip
from app.server import create_app


def audio(rate=22050):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as stream:
        stream.setparams((1,2,rate,0,'NONE','not compressed'))
        stream.writeframes(b'\0\0'*(rate//5))
    return buffer.getvalue()


def service(**extra):
    return Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9000/v1',tts_name='test-tts',**extra)


def mock_async(monkeypatch,handler):
    original=httpx.AsyncClient
    def client(**kwargs):
        return original(**kwargs,transport=httpx.MockTransport(handler))
    monkeypatch.setattr('app.model_backends.httpx.AsyncClient',client)


def test_legacy_settings_and_local_only_validation():
    defaults=Settings.model_validate({'translation_url':'http://localhost:1234/v1','index_version':'2.5'})
    assert defaults.asr_provider=='faster_whisper' and defaults.tts_provider=='indextts'
    assert defaults.translation_provider=='auto'
    assert catalog()['tts'][1]['reference_audio'] is False
    for field in ['asr_url','translation_url','tts_url']:
        for url in ['https://127.0.0.1/v1','http://example.com/v1','http://user:pass@localhost/v1','http://localhost/v1?key=x']:
            with pytest.raises(ValidationError):Settings(**{field:url})
    with pytest.raises(ValidationError):Settings(tts_provider='other')
    with pytest.raises(ValidationError):Settings(asr_provider='openai')
    with pytest.raises(ValidationError):service(tts_languages='unknown')
    assert service(tts_languages=' en,zh,en ').tts_languages=='en,zh'


def test_model_identity_and_capability_gate(tmp_path):
    settings=service();row=dict(id=1,zh='Hello',speaker='speaker_1')
    def clip(config,**extra):
        return make_voice_clip({**row,**extra},tmp_path,'',config,tts_model_signature(config),'ignored','en')
    original=clip(settings)
    assert original['output']==clip(settings,speaker='speaker_2')['output']
    for changes in [{'tts_name':'other'},{'tts_voice':'other'},{'tts_url':'http://localhost:9001/v1'},{'tts_revision':'v2'}]:
        assert original['output']!=clip(settings.model_copy(update=changes))['output']
    with pytest.raises(ValueError,match='情绪'):clip(settings,emotion='happy')
    with pytest.raises(ValueError,match='目标语言'):validate_tts(service(tts_languages='zh'),'ja')
    with pytest.raises(ValueError,match='2.5'):validate_tts(Settings(index_version='2'),'ja')
    assert chat_payload(settings.model_copy(update={'translation_provider':'openai','translation_url':'http://localhost/v1','translation_json_mode':False}),{'response_format':{},'chat_template_kwargs':{},'model':'abc'})=={'model':'abc'}


def test_transcription_requires_real_complete_word_alignment():
    source={'text':'Hello world.','words':[{'word':'Hello','start':.1,'end':.4},{'word':'world.','start':.5,'end':1.0}]}
    assert parse_transcription(source,1.0)[0]['text']=='Hello world.'
    for bad in [{'text':'Hello'},{**source,'text':'Hello missing world.'},
                {**source,'words':[{'word':'Hi','start':float('nan'),'end':.5}]},
                {**source,'words':[{'word':'Hi','start':1,'end':2}]}]:
        with pytest.raises(ValueError):parse_transcription(bad,1.0)


def test_transcription_request_and_failed_response_preserve_cache(tmp_path,monkeypatch):
    source=tmp_path/'input.wav';source.write_bytes(audio(16000));destination=tmp_path/'asr.json';destination.write_text('previous')
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={'text':'Hello','words':[{'word':'Hello','start':0,'end':.1}]})
    mock_async(monkeypatch,handler)
    settings=Settings(asr_provider='openai',asr_url='http://localhost:9000/v1',asr_name='test-asr')
    transcribe(settings,source,destination,'en','course',tmp_path/'log',threading.Event(),{})
    assert json.loads(destination.read_text())[0]['words'][0]['word']=='Hello'
    assert seen[0].url.path=='/v1/audio/transcriptions'
    assert b'verbose_json' in seen[0].content and b'timestamp_granularities[]' in seen[0].content
    destination.write_text('preserve')
    # The same transport can return an unaligned response without replacing the prior cache.
    monkeypatch.undo();mock_async(monkeypatch,lambda request:httpx.Response(200,json={'text':'no timestamps'}))
    with pytest.raises(ValueError,match='时间戳'):transcribe(settings,source,destination,'','course',tmp_path/'log',threading.Event(),{})
    assert destination.read_text()=='preserve'


def test_speech_api_atomic_normalized_audio_and_bridge_payload(tmp_path,monkeypatch):
    (tmp_path/'cache').mkdir();seen=[]
    def handler(request):seen.append(json.loads(request.content));return httpx.Response(200,content=audio())
    mock_async(monkeypatch,handler)
    output=tmp_path/'cache/test.wav';reference=tmp_path/'reference.wav';reference.write_bytes(audio())
    clip=dict(id=1,text='Hello',reference=str(reference),output=str(output),lang='EN')
    progress=[];synthesize_service(service(),[clip],1,tmp_path,threading.Event(),lambda *p:progress.append(p))
    assert seen[-1]==dict(model='test-tts',input='Hello',voice='default',response_format='wav')
    with wave.open(str(output)) as stream:assert stream.getparams()[:3]==(1,2,24000)
    assert .19<wav_duration(output)<.21 and progress==[(0,1),(1,1)]
    bridge=Settings(tts_provider='btv_reference',tts_url='http://localhost:9000',tts_name='bridge')
    synthesize_service(bridge,[{**clip,'emotion':'happy'}],1,tmp_path,threading.Event(),lambda *_:None)
    assert seen[-1]['protocol']=='btv-tts-v1' and seen[-1]['reference_audio']['data_base64']
    assert seen[-1]['emotion']['preset']=='happy'


def test_bad_or_redirected_audio_never_replaces_completed_clip(tmp_path,monkeypatch):
    output=tmp_path/'clip.wav';output.write_bytes(audio())
    original=output.read_bytes();clip=dict(text='Hello',output=str(output),lang='EN')
    for response in [httpx.Response(200,content=b'not a wav'),httpx.Response(302,headers={'Location':'https://example.com'})]:
        mock_async(monkeypatch,lambda request:response)
        with pytest.raises(ValueError):synthesize_service(service(),[clip],1,tmp_path,threading.Event(),lambda *_:None)
        assert output.read_bytes()==original and not output.with_suffix('.download').exists()
        monkeypatch.undo()


def test_request_cancellation_does_not_publish_partial_audio(tmp_path,monkeypatch):
    cancel=threading.Event();output=tmp_path/'clip.wav'
    async def handler(request):
        cancel.set();await asyncio.sleep(5);return httpx.Response(200,content=audio())
    mock_async(monkeypatch,handler)
    with pytest.raises(Cancelled):synthesize_service(service(),[dict(text='Hi',lang='EN',output=str(output))],1,tmp_path,cancel,lambda *_:None)
    assert not output.exists() and not output.with_suffix('.download').exists()


def test_draft_connection_check_does_not_save_or_infer(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');settings_path=tmp_path/'settings.json'
    monkeypatch.setattr('app.server.SETTINGS',settings_path)
    calls=[];original=httpx.Client
    def handler(request):calls.append(request);return httpx.Response(200,json={'data':[{'id':'test-tts'}]})
    with TestClient(app) as client:
        monkeypatch.setattr('app.model_backends.httpx.Client',lambda **kw:original(**kw,transport=httpx.MockTransport(handler)))
        result=client.post('/api/model-backends/check',json={'stage':'tts','settings':service().model_dump()})
        assert result.status_code==200,result.text
        assert result.json()['ok'] and result.json()['level']=='models'
        assert calls[0].method=='GET' and calls[0].url.path=='/v1/models' and len(calls)==1
        assert not settings_path.exists() and not app.state.pipeline.tts.status()['alive']
        assert client.get('/api/model-backends').json()['tts'][0]['reference_audio']
        assert client.post('/api/model-backends/check',json={'stage':'bad','settings':service().model_dump()}).status_code==400


def test_fixed_voice_audition_and_book_generation_without_reference(tmp_path,monkeypatch):
    mock_async(monkeypatch,lambda request:httpx.Response(200,content=audio()))
    app=create_app(tmp_path/'projects');pipeline=app.state.pipeline;settings=service()
    project=app.state.store.create('fixed voice','en','course');pid=project['id']
    row=dict(id=1,start=0,end=1,text='Hi',zh='Hello',speaker='speaker_1')
    folder=app.state.store.folder(pid);(folder/'cache').mkdir()
    app.state.store.update(pid,segments=[row],media={'duration':1},audition_request={'id':'draft','segment':row})
    try:
        pipeline.audition(pid,settings,threading.Event())
        assert app.state.store.read(pid)['audition']['file'] and not app.state.store.read(pid)['references']
        book=app.state.books.create('Book');chapter=app.state.books.add_chapter(book['id'],'Chapter')
        from app.audiobooks import split_prose
        chapter=app.state.books.save(book['id'],chapter['id'],{**chapter,'text':'Hello.','sentences':split_prose('Hello.'),'language':'en'})
        # No voice preset is needed for a service-managed fixed voice.
        app.state.narration.generate_chapter(book['id'],chapter['id'],settings,threading.Event(),None,0)
        ready=app.state.books.chapter(book['id'],chapter['id'])
        assert ready['audios'] and ready['outputs']['wav']
    finally:pipeline.close()
