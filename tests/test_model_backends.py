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

from app.config import BILIBILI_TRANSLATION_MODEL, BILIBILI_TRANSLATION_URL, Settings
from app.media import Cancelled, wav_duration
from app.model_backends import (catalog, chat_payload, check_backend, parse_transcription,
    synthesize_service, transcribe, tts_model_signature, validate_tts)
from app.pipeline import Pipeline, make_voice_clip
from app.server import create_app
from app.translation import local_translator
from app.translation_quality import run_workflow


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


def test_bilibili_preset_uses_official_endpoint_and_preserves_local_settings(monkeypatch, tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == BILIBILI_TRANSLATION_URL + '/chat/completions'
        payload = json.loads(request.content)
        assert payload['model'] == BILIBILI_TRANSLATION_MODEL
        assert payload['chat_template_kwargs'] == {'enable_thinking': False}
        if len(seen) > 1:
            assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={'choices': [{'message': {
            'content': '{"1":"Hello world."}',
        }}]})

    mock_async(monkeypatch, handler)
    settings = Settings(translation_provider='bilibili', translation_url='http://localhost:9000/v1',
                        translation_name='local-model', translation_review='off')
    assert settings.model_dump()['translation_name'] == 'local-model'
    assert settings.model_dump()['translation_url'] == 'http://localhost:9000/v1'
    for field in ('translation_url', 'asr_url', 'tts_url'):
        with pytest.raises(ValidationError):
            Settings(translation_provider='bilibili', **{field: 'https://example.com/v1'})
    probe = check_backend('translation', settings)
    assert probe['ok'] and probe['level'] == 'inference'
    probe_prompt = json.loads(seen[0].content)['messages'][-1]['content']
    probe_source = probe_prompt.split('〖源文〗\n',1)[1].split('\n\n',1)[0]
    assert json.loads(probe_source) == {'1': '你好，世界。'}
    rows = [dict(id=19, start=0, end=5, text='你好，世界。', zh='', speaker='speaker_1')]
    with local_translator(settings, tmp_path / 'process.log', threading.Event()) as url:
        output = run_workflow(url, settings, rows, [], 'course', threading.Event(),
                              topic={'language': 'zh', 'target_language': 'en'})
    assert output[19]['zh'] == 'Hello world.'
    assert len(seen) == 2


def test_bilibili_probe_reports_upstream_http_failure_without_following_redirect(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'https://example.com/v1/chat/completions'})

    mock_async(monkeypatch, handler)
    result = check_backend('translation', Settings(translation_provider='bilibili'))
    assert result['ok'] is False and 'HTTP 302' in result['message']
    assert len(seen) == 1


def test_bilibili_retries_invalid_translation_using_the_original_id_map(monkeypatch):
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)['messages'][0]['content'])
        text = 'Retry as needed.' if len(prompts) == 1 else 'Retry up to 3 times.'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'1': text})}}]})

    mock_async(monkeypatch, handler)
    settings = Settings(translation_provider='bilibili', translation_review='off')
    rows = [dict(id=19, start=0, end=5, text='最多重试3次。', zh='', speaker='speaker_1')]
    output = run_workflow(BILIBILI_TRANSLATION_URL, settings, rows, [], 'course', threading.Event(),
                          topic={'language': 'zh', 'target_language': 'en'})
    assert output[19]['zh'] == 'Retry up to 3 times.'
    assert len(prompts) == 2 and 'Retry as needed.' in prompts[1]


def test_bilibili_settings_roundtrip_keeps_local_fallback(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    monkeypatch.setattr('app.server.SETTINGS', path)
    monkeypatch.setattr('app.config.SETTINGS', path)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        local = dict(translation_provider='openai', translation_url='http://127.0.0.1:8000/v1',
                     translation_name='local-chat')
        cloud = {**local, 'translation_provider': 'bilibili'}
        assert client.put('/api/settings', json=cloud).status_code == 200
        saved = client.get('/api/settings').json()
        assert {key: saved[key] for key in cloud} == cloud
        assert not path.read_bytes().startswith(b'\xef\xbb\xbf')
        assert client.get('/api/diagnostics').json()['local_only'] is False
        assert client.put('/api/settings', json=local).status_code == 200
        assert client.get('/api/diagnostics').json()['local_only'] is True


def test_local_translation_catalog_finds_nested_models_and_first_shards(tmp_path, monkeypatch):
    models = tmp_path / 'models'
    nested = models / 'downloaded'
    nested.mkdir(parents=True)
    for path in [models / 'New.GGUF', nested / 'Fine-00001-of-00002.gguf',
                 nested / 'Fine-00002-of-00002.gguf', models / 'Incomplete.gguf.partial']:
        path.touch()
    (models / 'folder.gguf').mkdir()
    app = create_app(tmp_path / 'projects')
    monkeypatch.setattr('app.server.ROOT', tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/model-backends/local-translation').json() == [
            'models/downloaded/Fine-00001-of-00002.gguf', 'models/New.GGUF'
        ]


def test_network_translation_preserves_and_uses_key_only_for_saved_url(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    monkeypatch.setattr('app.server.SETTINGS', path)
    monkeypatch.setattr('app.config.SETTINGS', path)
    seen = []

    def handler(request):
        seen.append(request)
        payload = json.loads(request.content)
        assert payload['model'] == 'test-chat'
        assert 'chat_template_kwargs' not in payload
        assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={'choices': [{'message': {'content':
            '{"translations":[{"id":1,"translation":"你好。"}]}'}}]})

    mock_async(monkeypatch, handler)
    config = dict(translation_provider='openai_remote', translation_api_url='https://example.com/v1/chat/completions',
                  translation_name='test-chat', translation_review='off', translation_compact=False,
                  translation_remote_batch=100)
    key = 'synthetic-api-key'
    with TestClient(create_app(tmp_path / 'projects')) as client:
        resp = client.put('/api/settings', json={**config, 'translation_api_key': key})
        assert resp.status_code == 200 and key not in resp.text
        assert resp.json()['translation_api_key_configured'] is True
        assert resp.json()['translation_api_url'] == 'https://example.com/v1'
        assert client.get('/api/settings').json()['translation_remote_batch'] == 100
        invalid = client.put('/api/settings', json={**config, 'translation_api_url': 'http://example.com',
                                                     'translation_api_key': key})
        assert invalid.status_code == 422 and key not in invalid.text
        assert client.put('/api/settings', json=config).json()['translation_api_key_configured']
        assert client.put('/api/tts-session/retention', json={'tts_idle_minutes': 0,
            'tts_keep_loaded': False}).json()['settings']['translation_api_key_configured']
        assert not path.read_bytes().startswith(b'\xef\xbb\xbf')
        assert client.get('/api/diagnostics').json()['local_only'] is False

        for override in ({}, {'translation_api_url': 'https://another.example/v1'}):
            response = client.post('/api/model-backends/check', json={'stage': 'translation', 'settings': {**config, **override}})
            assert response.json()['ok'] is True

        from app.config import load_settings
        from app.project_experience import effective_asr
        from app.translation import log_network_batch
        settings = effective_asr(load_settings(), {'asr_options': {'asr_device': 'cpu'}})
        rows = [dict(id=1, start=0, end=10, text='Hello.', zh='', speaker='speaker_1')]
        logs = []
        with log_network_batch(settings, [1], logs.append):
            translated = run_workflow(settings.translation_service_url, settings, rows, [], 'course', threading.Event())
        assert translated[1]['zh'] == '你好。'
        assert len(logs) == 1 and '字幕翻译 1 次' in logs[0] and key not in logs[0]
        assert [request.headers.get('authorization') for request in seen] == ['Bearer ' + key, None, 'Bearer ' + key]
        assert [str(request.url) for request in seen] == [
            'https://example.com/v1/chat/completions', 'https://another.example/v1/chat/completions',
            'https://example.com/v1/chat/completions',
        ]
        assert not client.put('/api/settings', json={**config, 'translation_api_key': ''}).json()['translation_api_key_configured']


def test_siliconflow_deepseek_translation_disables_thinking_only_on_known_route():
    remote = Settings(translation_provider='openai_remote', translation_api_url='https://api.siliconflow.cn/v1',
                      translation_name='deepseek-ai/DeepSeek-V3.2')
    assert chat_payload(remote, {'chat_template_kwargs': {}}) == {'enable_thinking': False}
    assert chat_payload(remote.model_copy(update={'translation_api_url': 'https://another.example/v1'}), {}) == {}
    assert chat_payload(remote.model_copy(update={'translation_name': 'another-chat-model'}), {}) == {}


def test_network_request_trace_displays_partial_stream_and_masks_credentials(monkeypatch):
    from app.translation import _post_cancellable
    from app.translation_requests import TranslationRequests, request_scope

    history = TranslationRequests()
    key = 'synthetic-stream-key'
    first_read = asyncio.Event()
    continue_stream = asyncio.Event()

    def event(choice):
        return ('data: '+json.dumps({'choices':[choice]})+'\n\n').encode()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({'delta':{'reasoning_content':'Provider reasoning'},'index':0})
            yield event({'delta':{'content':'Partial '},'index':0})
            first_read.set()
            await continue_stream.wait()
            yield event({'delta':{'content':'subtitle'},'index':0,'finish_reason':'stop'})
            yield b'data: [DONE]\n\n'

    def handler(request):
        assert request.headers['authorization'] == 'Bearer '+key
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Body())

    mock_async(monkeypatch, handler)

    async def check():
        token = request_scope.set((history,'project','run',True))
        try:
            request = asyncio.create_task(_post_cancellable('https://example.com/v1/chat/completions',
                {'model':'fixture','stream':False,'messages':[{'role':'user','content':'Source '+key}]},
                threading.Event(),network=True,api_key=key,phase='段落翻译'))
            try:
                await asyncio.wait_for(first_read.wait(), 2)
                partial = history.snapshot('project','run')['selected']
                assert partial['content'] == 'Partial ' and partial['finished'] is None
                assert 'reasoning_content' in partial['response'] and partial['first_response'] is not None
                assert key not in partial['request'] and json.loads(partial['request'])['stream']
                assert history.snapshot('another-project','run')['selected'] is None
            finally:
                continue_stream.set()
            response = await asyncio.wait_for(request, 2)
            assert response.json()['choices'][0]['message']['content'] == 'Partial subtitle'
            assert history.snapshot('project','run')['selected']['finished'] is not None
        finally:
            request_scope.reset(token)

    asyncio.run(check())

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
