from hashlib import sha256
from contextlib import nullcontext
import json
from pathlib import Path
import struct
from threading import Event
import time
import wave
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx
import pytest

from app.nier_audio import ALIGNMENT, STREAM_INDEX, WAI_MAGIC, catalogue, make_mod, stream_index
from app.config import Settings, atomic_json
from app.game_voice import GameVoiceProjects, spoken_text, voice_for
from app.pipeline import Pipeline
from app.server import create_app


def fixture_wem(payload):
    fmt = struct.pack('<HHIIHH', 0xffff, 1, 48000, 0, 0, 0)
    body = b'WAVEfmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(payload)) + payload
    return b'RIFF' + struct.pack('<I', len(body)) + body


def fixture_bank(media_id):
    sound = struct.pack('<I', 0x40001) + b'\x02' + struct.pack('<I', media_id)
    action = b'\x03\x04' + struct.pack('<I', 99)
    event = struct.pack('<II', 1, 100)
    hirc = struct.pack('<I', 3)
    for kind, object_id, body in ((2, 99, sound), (3, 100, action), (4, 101, event)):
        hirc += struct.pack('<BII', kind, 4 + len(body), object_id) + body
    header = struct.pack('<I', 113)
    return b'BKHD' + struct.pack('<I', len(header)) + header + b'HIRC' + struct.pack('<I', len(hirc)) + hirc


@pytest.fixture
def game(tmp_path):
    root = tmp_path / 'game'
    sound = root / 'data' / 'sound'
    original = fixture_wem(b'original media marker')
    table = struct.pack('<4I', WAI_MAGIC, 2, 1, 3)
    for directory, start, end in ((b'Japanese', 0, 2), (b'English(US)', 2, 3)):
        table += struct.pack('<16s4I', directory, 0, 0, start, end)
    table += struct.pack('<16s4I', b'vo_core', 0, 0, 0, 0)
    for media, offset in ((1, 0), (2, ALIGNMENT), (3, 0)):
        table += struct.pack('<IIIHH', media, len(original), offset, 0, 0)
    for directory, media in (('Japanese', 1), ('English(US)', 3)):
        (sound / directory).mkdir(parents=True)
        (sound / directory / 'vo_core.bnk').write_bytes(fixture_bank(media))
        stream = sound / 'stream' / directory
        stream.mkdir(parents=True)
        wsp = original.ljust(ALIGNMENT, b'\0') + original if media == 1 else original
        (stream / 'vo_core_0_000.wsp').write_bytes(wsp)
    (sound / STREAM_INDEX).write_bytes(table)
    (root / 'NieRAutomata.exe').touch()
    return root, sound


def test_catalogue_links_languages_by_bank_object_and_keeps_unmatched(game):
    _, sound = game
    rows = catalogue(sound, 'ja')['entries']
    assert len(rows) == 2
    assert rows[0]['assets']['en']['media'] == 3
    assert rows[0]['events'] == [101]
    assert 'en' not in rows[1]['assets']
    assert rows[1]['assets']['ja']['offset'] == ALIGNMENT


def test_mod_contains_only_replacements_and_round_trips_wai(game, tmp_path):
    _, sound = game
    before = {path: sha256(path.read_bytes()).hexdigest() for path in sound.rglob('*') if path.is_file()}
    first, second = fixture_wem(b'Chinese line 1'), fixture_wem(b'Chinese line 2')
    replacements = {}
    for media, audio in ((1, first), (2, second)):
        wem = tmp_path / f'{media}.wem'
        wem.write_bytes(audio)
        replacements[media] = dict(wem=wem, label=str(media), tested=False)
    destination = tmp_path / 'mod.zip'
    make_mod(sound, before[sound / STREAM_INDEX], 'ja', 'Test package', replacements, destination, Event(), build='test')
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        _, _, entries = stream_index(archive.read(STREAM_INDEX))
        assert [(entry.media_id, entry.offset) for entry in entries] == [(1, 0), (2, ALIGNMENT)]
        wsp = archive.read(entries[0].path)
        assert b'original media marker' not in wsp and wsp[:len(first)] == first
        assert wsp[ALIGNMENT:ALIGNMENT + len(second)] == second
        raw = archive.read('audioModsMetadata.json')
        assert not raw.startswith(b'\xef\xbb\xbf')
        assert set(json.loads(raw)['moddedWaiChunks']) == {'1', '2'}
        assert json.loads(archive.read('manifest.json'))['game_build'] == 'test'
    assert all(sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items())
    with pytest.raises(ValueError, match='索引已变化'):
        make_mod(sound, 'invalid', 'ja', 'Test package', replacements, destination, Event())


def test_game_voice_api_checks_edits_and_prevents_unverified_release(game, tmp_path):
    root, _ = game
    with TestClient(create_app(tmp_path / 'projects')) as client:
        created = client.post('/api/game-voices', json=dict(title='Test', language='ja', path=str(root)))
        assert created.status_code == 200
        project = created.json()
        url = '/api/game-voices/' + project['id']
        rows = client.get(url + '/rows?status=replaceable').json()['rows']
        assert len(rows) == 2
        line_url = url + '/rows/1'
        save = client.put(line_url, json=dict(revision=0, text='测试配音', ja='test'))
        assert save.status_code == 200
        assert save.json()['ja_source'] == 'manual'
        assert client.put(line_url, json=dict(revision=0, text='old version')).status_code == 409
        assert client.get(url + '/rows?q=101').json()['total'] == 1
        assert client.post(line_url + '/test-result', json=dict(revision=1, tested=True)).status_code == 400
        assert client.post(url + '/jobs', json=dict(action='package', rows=['1'], revision=1, release=True)).status_code == 400


def test_deleting_candidates_preserves_other_versions_and_resets_adopted_choice(game, tmp_path):
    root, _ = game
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', path=str(root))).json()
        folder = tmp_path / 'game_projects' / project['id']
        state_path = folder / 'project.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        chosen, alternate = 'a' * 24, 'b' * 24
        state['rows']['1'] = dict(selected=chosen, tested=True, candidates={
            chosen: {'file': 'shared.wav'}, alternate: {'file': 'shared.wav'},
        })
        atomic_json(state_path, state)
        cache = folder / 'cache' / 'shared.wav'
        cache.write_bytes(b'shared candidate cache')
        line = f"/api/game-voices/{project['id']}/rows/1"
        def remove(candidate, revision):
            return client.request('DELETE', f'{line}/candidates/{candidate}', json={'revision': revision})
        result = remove(alternate, project['revision'])
        assert result.status_code == 200
        row = result.json()
        assert list(row['candidates']) == [chosen] and row['tested'] and row['selected'] == chosen
        assert remove(chosen, project['revision']).status_code == 409
        assert client.get(f'{line}/candidates/{chosen}').content == cache.read_bytes()
        result = remove(chosen, row['revision'])
        assert result.status_code == 200
        row = result.json()
        assert row['decision'] == 'pending' and not row['selected'] and not row['tested'] and not row['candidates']
        assert client.get(f'{line}/candidates/{chosen}').status_code == 404
        assert remove(chosen, row['revision']).status_code == 404


def test_mixed_release_preview_and_package_keep_original_wem(game, tmp_path, monkeypatch):
    root, _ = game
    monkeypatch.setattr('app.game_voice.tools', lambda: (None, 'encoder', 'template'))
    monkeypatch.setattr('app.game_voice.encode_wem', lambda _encoder, _template, _wav, output, *_:
                        output.write_bytes(fixture_wem(b'Chinese candidate')))
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        folder = tmp_path / 'game_projects' / project['id']
        path = folder / 'project.json'
        candidate = 'a' * 24
        state = json.loads(path.read_text(encoding='utf-8'))
        state['rows'] = {
            '1': dict(scene='Scene', scene_order=2, selected=candidate, tested=True, text='中文',
                      candidates={candidate: dict(file='candidate.wav')}),
            '2': dict(scene='Scene', scene_order=1, text='保留原版'),
        }
        atomic_json(path, state)
        with wave.open(str(folder / 'cache' / 'candidate.wav'), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<h', 5000) * 12000)
        chosen = client.put(url + '/original-selection', json=dict(rows=['1', '2'], original=True,
                            protect_adopted=True, revision=project['revision']))
        assert chosen.status_code == 200 and chosen.json()['changed'] == 1
        revision = chosen.json()['project']['revision']
        ordered = client.get(url + '/rows?scene=Scene').json()['rows']
        assert [(row['id'], row['decision']) for row in ordered] == [('2', 'original'), ('1', 'chinese')]
        assert [row['id'] for row in client.get(url + '/rows?status=original').json()['rows']] == ['2']
        payload = dict(rows=['1', '2'], release=True, revision=revision)
        preview = client.post(url + '/package-preview', json=payload)
        assert [row['status'] for row in preview.json()['rows']] == ['replace', 'original']
        assert client.post(url + '/jobs', json={**payload, 'action': 'package'}).status_code == 200
        for _ in range(120):
            detail = client.get(url).json()
            if detail['job']['status'] in {'done', 'failed'}:
                break
            time.sleep(.025)
        assert detail['job']['status'] == 'done', detail['job']
        with ZipFile(folder / 'packages' / (detail['packages'][0]['id'] + '.zip')) as archive:
            _, _, entries = stream_index(archive.read(STREAM_INDEX))
            assert [entry.media_id for entry in entries] == [1]
            assert b'original media marker' not in archive.read(entries[0].path)


def test_voice_conversion_retries_only_unfinished_clips_on_cuda_error(tmp_path, monkeypatch):
    from app import voice_conversion

    monkeypatch.setattr(voice_conversion, 'available', lambda _settings: True)
    requests = []
    fallbacks = []

    def fake_worker(command, *_args, **_kwargs):
        request = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
        requests.append(request)
        clip = request['clips'][0]
        with wave.open(clip['output'], 'wb') as output:
            output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x00\x01' * 100)
        if len(requests) == 1:
            raise RuntimeError('CUDA error: unknown error')

    monkeypatch.setattr(voice_conversion, 'run', fake_worker)
    clips = [dict(id=id, source=tmp_path / 'source.wav', target=tmp_path / 'target.wav', output=tmp_path / f'{id}.wav')
             for id in ('completed', 'unfinished')]
    voice_conversion.convert(Settings(), clips, tmp_path / 'work', tmp_path / 'process.log', Event(),
                             on_fallback=lambda: fallbacks.append('cpu'))
    assert [clip['id'] for clip in requests[0]['clips']] == ['completed', 'unfinished']
    assert requests[1]['device'] == 'cpu' and [clip['id'] for clip in requests[1]['clips']] == ['unfinished']
    assert fallbacks == ['cpu']


def test_role_voice_precedence_and_save(game, tmp_path, monkeypatch):
    assert voice_for(dict(voice='line', role='2B'), 'batch', {'2B': 'role'}) == 'line'
    assert voice_for(dict(voice='', role='2B'), 'batch', {'2B': 'role'}) == 'role'
    assert voice_for(dict(voice='', role='9S'), 'batch', {'2B': 'role'}) == 'batch'
    root, _ = game
    monkeypatch.setattr(GameVoiceProjects, 'voice', lambda _self, value: dict(id=value, name=value))
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', language='ja', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        assert client.put(url + '/rows/1', json=dict(revision=0, role='2B')).status_code == 200
        assert client.put(url + '/rows/2', json=dict(revision=1, role='9S')).status_code == 200
        saved = client.put(url + '/role-voices', json=dict(revision=2, role_voices={'2B': 'voice-a', '9S': 'voice-b'}))
        assert saved.status_code == 200
        assert saved.json()['role_voices'] == {'2B': 'voice-a', '9S': 'voice-b'}
        assert client.put(url + '/role-voices', json=dict(revision=2, role_voices={})).status_code == 409
        assert client.put(url + '/role-voices', json=dict(revision=3, role_voices={'Unknown': 'voice-a'})).status_code in (400, 422)


def test_batch_override_ignores_line_voice_and_source_emotion_and_skips_punctuation(game, tmp_path, monkeypatch):
    root, _ = game
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', 100, -100) * 6000)
    line_voice, batch_voice = 'a' * 32, 'b' * 32
    for voice, name in ((line_voice, 'Game voice'), (batch_voice, 'Mandarin')):
        folder = tmp_path / 'voices' / voice
        folder.mkdir(parents=True)
        atomic_json(folder / 'voice.json', dict(name=name, audio='voice.wav'))
        (folder / 'voice.wav').write_bytes(source.read_bytes())
    monkeypatch.setattr('app.game_voice_api.load_settings', lambda: Settings())
    monkeypatch.setattr(GameVoiceProjects, 'source', lambda *_args: source)
    monkeypatch.setattr(GameVoiceProjects, 'emotion_reference', lambda *_args: pytest.fail('Used original emotion'))
    clips = []

    def synthesize(_pipeline, _id, pending, _total, _settings, _event, _stage):
        clips.extend(pending)
        for clip in pending:
            Path(clip['output']).write_bytes(source.read_bytes())

    monkeypatch.setattr(Pipeline, 'synthesize', synthesize)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        assert client.put(url + '/rows/1', json=dict(revision=0, text='了解。', voice=line_voice, emotion='source')).status_code == 200
        assert client.put(url + '/rows/2', json=dict(revision=1, text='……')).status_code == 200
        response = client.post(url + '/jobs', json=dict(action='generate', rows=['1', '2'], revision=2,
                                                         voice=batch_voice, emotion='inherit', force_batch_settings=True))
        assert response.status_code == 200
        for _ in range(100):
            job = client.get(url).json()['job']
            if job['status'] in {'done', 'failed'}:
                break
            time.sleep(.02)
        assert job['status'] == 'done' and job['done_rows'] == ['1'] and job['skipped_rows'] == ['2']
        assert [(clip['id'], clip.get('emotion_reference')) for clip in clips] == [('1', None)]
        row = client.get(url + '/rows/1').json()
        candidate, = row['candidates'].values()
        assert candidate['voice'] == 'Mandarin' and candidate['synthesis']['voice_id'] == batch_voice
        assert candidate['synthesis']['emotion'] == 'inherit' and row['emotion'] == 'source'
        assert row['voice'] == line_voice and not row['selected']


def test_drama3_job_builds_independent_candidates_and_uses_original_reference(game, tmp_path, monkeypatch):
    root, _ = game
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', 100, -100) * 6000)
    cli = tmp_path / 'audiocpp_cli.exe'
    cli.touch()
    weights = tmp_path / 'model.gguf'
    asr_model = tmp_path / 'asr'
    asr_model.mkdir()
    (asr_model / 'model.bin').touch()
    settings = Settings(drama3_executable=str(cli), drama3_model=str(weights), asr_model=str(asr_model))
    monkeypatch.setattr('app.game_voice_api.load_settings', lambda: settings)
    monkeypatch.setattr(GameVoiceProjects, 'source', lambda *_args: source)
    monkeypatch.setattr(GameVoiceProjects, 'emotion_reference', lambda *_args: source)
    monkeypatch.setattr('app.game_voice.validate_tts', lambda *_args: pytest.fail('Used the global TTS backend'))
    monkeypatch.setattr('app.drama3.JapaneseOutputCheck.reject', lambda *_args: False)
    calls = []

    def run(command, *_args, **_kwargs):
        calls.append(command)
        Path(command[command.index('--out') + 1]).write_bytes(source.read_bytes())

    monkeypatch.setattr('app.drama3.run', run)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        tools_url = '/api/game-voices/tools'
        assert client.get(tools_url).json()['drama3'] is False
        weights.write_bytes(b'GGUF')
        assert client.get(tools_url).json()['drama3'] is True
        project = client.post('/api/game-voices', json=dict(title='Drama', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        line_url = url + '/rows/1'
        line = dict(text='2B，跟我来。', ja='一緒に来て。', emotion='source', drama3_direction='低声说')
        response = client.put(line_url, json=dict(revision=0, **line))
        assert response.status_code == 200
        assert client.put(url + '/rows/2', json=dict(revision=1, text='请等一下。')).status_code == 200

        def submit(action, rows, revision, route='drama3', expected='done'):
            response = client.post(url + '/jobs', json=dict(action=action, rows=rows, revision=revision, route=route))
            assert response.status_code == 200, response.text
            for _ in range(150):
                state = client.get(url).json()
                if state['job']['status'] in {'done', 'failed'}:
                    break
                time.sleep(.02)
            assert state['job']['status'] == expected, state['job']
            return state

        state = submit('generate', ['1', '2'], 2)
        first_state = client.get(line_url).json()
        first_id, = first_state['candidates']
        assert first_state['candidates'][first_id]['synthesis']['source_language'] == 'ja'
        assert calls[0][calls[0].index('--task') + 1] == 'clon'
        assert '[用标准普通话说下面的中文台词，保留参考音的音色与表演，低声说] two b，跟我来。' in calls[0]
        assert 'reference_text=一緒に来て。' in calls[0]
        assert calls[1][calls[1].index('--task') + 1] == 'tts'
        assert state['job']['done_rows'] == ['1', '2']
        monkeypatch.setattr('app.drama3.Drama3Session.warm', lambda *_args: True)
        assert client.get(tools_url).json()['warm_model'] == 'Drama 3'
        state = submit('alternate', ['1'], state['revision'], route='warm')
        assert state['job']['options']['route'] == 'drama3'
        candidates = client.get(line_url).json()['candidates']
        assert len(candidates) == 2 and first_id in candidates
        assert len({item['synthesis']['seed'] for item in candidates.values()}) == 2
        edited = client.put(line_url, json=dict(revision=state['revision'], **{k: v for k, v in line.items() if k != 'drama3_direction'}))
        assert edited.json()['drama3_direction'] == '低声说'
        monkeypatch.setattr('app.drama3.JapaneseOutputCheck.reject', lambda *_args: True)
        failed = submit('alternate', ['1'], edited.json()['revision'], expected='failed')
        assert '已拦截' in failed['job']['error']
        assert client.get(line_url).json()['candidates'] == candidates


def test_drama3_conversion_keeps_mandarin_and_target_references_separate(game, tmp_path, monkeypatch):
    root, _ = game
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', 100, -100) * 6000)
    mandarin, original = 'a' * 32, 'b' * 32
    audio = {}
    for voice_id, name, sample in ((mandarin, '普通话', 100), (original, '角色原声', 200)):
        folder = tmp_path / 'voices' / voice_id
        folder.mkdir(parents=True)
        audio[voice_id] = folder / 'voice.wav'
        with wave.open(str(audio[voice_id]), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<hh', sample, -sample) * 6000)
        atomic_json(folder / 'voice.json', dict(name=name, audio='voice.wav'))

    cli, weights = tmp_path / 'audiocpp_cli.exe', tmp_path / 'weights.gguf'
    cli.touch()
    weights.write_bytes(b'GGUF')
    settings = Settings(drama3_executable=str(cli), drama3_model=str(weights), vc_provider='seed_vc')
    monkeypatch.setattr('app.game_voice_api.load_settings', lambda: settings)
    monkeypatch.setattr(GameVoiceProjects, 'source', lambda *_args: source)
    monkeypatch.setattr(GameVoiceProjects, 'emotion_reference', lambda *_args: pytest.fail('Conditioned on the original line'))
    calls = []

    def draft(_settings, text, reference, transcript, _seed, output, _log, _cancel):
        assert text == '[自然、清晰的标准普通话，轻声询问] 要不要去补给一下？'
        assert transcript == '普通话参考音。'
        assert reference.read_bytes() == audio[mandarin].read_bytes()
        output.write_bytes(source.read_bytes())
        calls.append('drama3')

    def convert(_settings, clips, *_args, **_kwargs):
        clip, = clips
        assert clip['target'] == audio[original]
        assert clip['source'].read_bytes() == source.read_bytes()
        clip['output'].write_bytes(audio[original].read_bytes())
        calls.append('seed-vc')

    monkeypatch.setattr('app.drama3.generate', draft)
    monkeypatch.setattr('app.voice_conversion.convert', convert)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Conversion', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        line = dict(text='要不要去补给一下？', role='2B', emotion='source', drama3_direction='轻声询问',
                    drama3_reference_text='普通话参考音。')
        assert client.put(url + '/rows/1', json=dict(revision=0, **line)).status_code == 200
        assert client.put(url + '/role-voices', json=dict(revision=1, role_voices={'2B': original})).status_code == 200
        job = dict(action='generate', rows=['1'], revision=2, route='drama3_conversion')
        voice_record = audio[original].with_name('voice.json')
        active = json.loads(voice_record.read_text('utf-8'))
        atomic_json(voice_record, {**active, 'archived': True})
        archived = client.post(url + '/jobs', json=dict(**job, voice=mandarin))
        assert archived.status_code == 400
        assert '角色「2B」绑定的变声音色「角色原声」已归档' in archived.json()['detail']
        atomic_json(voice_record, active)
        rejected = client.post(url + '/jobs', json=dict(**job, voice=original))
        assert rejected.status_code == 400 and '普通话参考音' in rejected.json()['detail']
        submitted = client.post(url + '/jobs', json=dict(**job, voice=mandarin))
        assert submitted.status_code == 200, submitted.text
        for _ in range(150):
            state = client.get(url).json()['job']
            if state['status'] in {'done', 'failed'}:
                break
            time.sleep(.02)
        assert state['status'] == 'done', state
        assert calls == ['drama3', 'seed-vc']
        row = client.get(url + '/rows/1').json()
        candidate, = row['candidates'].values()
        synthesis = candidate['synthesis']
        assert (synthesis['method'], synthesis['voice_id'], synthesis['vc_target'], synthesis['source_language']) == (
            'drama3_conversion', mandarin, original, '')
        assert candidate['performance_file'] and row['emotion'] == 'source'


def test_role_reference_skips_sound_notes_and_saves_manual_pick_without_rebinding(game, tmp_path, monkeypatch):
    root, _ = game
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', 100, -100) * 48000)
    monkeypatch.setattr(GameVoiceProjects, '_reference_piece', lambda *_args: source)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Pod', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        edit = client.put(url + '/rows/1', json=dict(revision=0, role='Pod 042', ja='プログラムでランダムな記号を表示'))
        assert edit.status_code == 200
        spoken = client.put(url + '/rows/2', json=dict(revision=1, role='Pod 042', ja='報告。'))
        assert spoken.status_code == 200
        auto = client.post(url + '/roles/Pod%20042/voice-from-source', json=dict(revision=2))
        assert auto.status_code == 200
        assert auto.json()['job']['row_ids'] == ['2']

        def finished():
            for _ in range(150):
                detail = client.get(url).json()
                if detail['job']['status'] in {'done', 'failed'}:
                    break
                time.sleep(.02)
            assert detail['job']['status'] == 'done', detail['job']
            return detail

        bound = finished()
        initial_voice = bound['role_voices']['Pod 042']
        job = dict(action='voice_from_selection', revision=bound['revision'])
        rejected = client.post(url + '/jobs', json=dict(**job, rows=['1', '2']))
        assert rejected.status_code == 400
        accepted = client.post(url + '/jobs', json=dict(**job, rows=['2']))
        assert accepted.status_code == 200
        result = finished()
        new_voice = result['job']['voice_result']
        assert result['role_voices']['Pod 042'] == initial_voice != new_voice['id']
        assert new_voice['name'] == 'Pod 042（选段 2）'
        assert client.get('/api/voices/' + new_voice['id'] + '/audio').content.startswith(b'RIFF')


def test_import_game_text_fills_blanks_and_keeps_existing_edits(game, tmp_path, monkeypatch):
    root, _ = game
    monkeypatch.setattr('app.game_voice.dialogue_lines', lambda: {
        '1': dict(ja='警告：敵兵器接近。', en='Alert: enemy unit approaching.', zh='警告：敌方兵器接近。',
                  role='Pod 042', source='TextSource.ruby'),
        '2': dict(ja='', en='', zh='', role='2B', source='TextSource.ruby'),
    })
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', language='ja', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        imported = client.post(url + '/import-text', json=dict(revision=0)).json()
        assert imported['matched'] == 2 and imported['filled'] == 2 and imported['chinese'] == 1
        row = client.get(url + '/rows/1').json()
        assert row['ja'] == '警告：敵兵器接近。'
        assert row['en'] == 'Alert: enemy unit approaching.'
        assert row['text'] == '警告：敌方兵器接近。'
        assert row['role'] == 'Pod 042'
        assert row['ja_source'] == row['en_source'] == row['text_source'] == 'game'
        assert client.get(url + '/rows/2').json()['role'] == '2B'

        revision = imported['revision']
        saved = client.put(url + '/rows/1', json=dict(revision=revision, text='人工改写', ja=row['ja'],
                                                    en=row['en'], role=row['role']))
        assert saved.status_code == 200 and saved.json()['text_source'] == 'manual'
        revision = saved.json()['revision']
        again = client.post(url + '/import-text', json=dict(revision=revision)).json()
        assert again['filled'] == 0 and again['matched'] == 2
        assert client.get(url + '/rows/1').json()['text'] == '人工改写'


def test_spoken_script_suggestion_preserves_adopted_audio_until_script_is_saved(game, tmp_path, monkeypatch):
    root, _ = game
    source = '無駄だ。'
    old_text, new_text = '那是徒劳无功的。', '白费力气。'
    calls = []

    async def post(url, payload, cancel, **_options):
        messages = payload['messages']
        calls.append(messages)
        reply = dict(translations=[dict(id=1, zh=new_text)])
        return httpx.Response(200, json=dict(choices=[dict(message=dict(content=json.dumps(reply)))]),
                              request=httpx.Request('POST', url))

    monkeypatch.setattr('app.game_voice_api.load_settings', lambda: Settings())
    monkeypatch.setattr('app.game_voice.local_translator', lambda *_args: nullcontext('http://localhost'))
    monkeypatch.setattr('app.translation._post_cancellable', post)
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        line_url = url + '/rows/1'
        path = tmp_path / 'game_projects' / project['id'] / 'project.json'
        state = json.loads(path.read_bytes())
        candidate = {'duration': 1.3}
        state['rows']['1'] = dict(ja=source, text=old_text, source_duration=1.2, selected='current',
                                  tested=True, candidates={'current': candidate})
        atomic_json(path, state)

        assert client.post(url + '/jobs', json=dict(action='polish', rows=['1', '2'], revision=0)).status_code == 200
        for _ in range(100):
            job = client.get(url).json()['job']
            if job['status'] in {'done', 'failed'}:
                break
            time.sleep(.02)
        assert job['status'] == 'done' and job['done_rows'] == ['1']
        assert '游戏台词' in calls[0][0]['content']
        cue, = json.loads(calls[0][1]['content'].removesuffix('\n/no_think'))['segments']
        assert (cue['text'], cue['draft'], cue['seconds']) == (source, old_text, 1.2)

        row = client.get(line_url).json()
        assert (row['text'], row['suggested_text'], row['selected'], row['tested']) == (old_text, new_text, 'current', True)
        assert client.get(url + '/rows?status=has-suggestion').json()['total'] == 1
        saved = client.put(line_url, json=dict(revision=row['revision'], text=new_text, ja=source)).json()
        assert saved['text'] == new_text and not saved['suggested_text'] and not saved['selected']
        assert saved['candidates'] == {'current': candidate}
        assert client.get(url + '/rows?status=has-suggestion').json()['total'] == 0


@pytest.mark.parametrize('voice_frames', [12000, 33600])
def test_render_candidate_keeps_non_center_channels_with_background(tmp_path, voice_frames):
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((6, 2, 48000, 0, 'NONE', 'not compressed'))
        import math
        frames = bytearray()
        for index in range(48000):
            values = [int(8000 * math.sin(2 * math.pi * 440 * index / 48000))] * 6
            values[2] = int(4000 * math.sin(2 * math.pi * 220 * index / 48000))
            frames.extend(struct.pack('<6h', *values))
        stream.writeframes(bytes(frames))
    voice = tmp_path / 'voice.wav'
    with wave.open(str(voice), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 5000) * voice_frames)
    cache = tmp_path / 'project' / 'cache'
    cache.mkdir(parents=True)
    row = dict(trim_start=0, trim_end=0, speed=1, gain=0, lead=0, tail=0, preserve_background=True)

    class Idle:
        def busy(self, _key): return False

    projects = GameVoiceProjects(tmp_path / 'game_projects', Idle(), None)
    output, _stamp, package_file, voice_duration = projects.render_candidate(
        row, voice, Settings(), cache, cache / 'process.log', Event(), source)
    assert output.is_file() and package_file and package_file.is_file()
    assert voice_duration == pytest.approx(voice_frames / 24000, abs=.001)
    with wave.open(str(package_file), 'rb') as stream:
        assert stream.getnchannels() == 6 and stream.getframerate() == 48000
        data = stream.readframes(stream.getnframes())
    first = struct.unpack_from('<6h', data, 1000 * 12)
    assert first[0] and first[1] and first[4] and first[5]
    assert len(data) // 12 == max(48000, voice_frames * 2)
    if voice_frames > 24000:
        late = struct.unpack_from('<6h', data, 55000 * 12)
        assert late == (0, 0, 5000, 0, 0, 0)
    with wave.open(str(output), 'rb') as stream:
        assert stream.getnchannels() == 1 and stream.getframerate() == 24000


def test_local_game_dialogue_lookup_is_optional(tmp_path, monkeypatch):
    from app import nier_text

    lookup = tmp_path / 'dialogue.json'
    monkeypatch.setattr(nier_text, 'DATA', lookup)
    monkeypatch.setattr(nier_text, 'LEGACY', tmp_path / 'dialogue.json.gz')
    nier_text.dialogue_lines.cache_clear()
    try:
        assert nier_text.dialogue_lines() == {}
        assert nier_text.dialogue_source() == ''
        sample = {'123': {'ja': '試験です。', 'zh': '这是一条测试台词。'}}
        atomic_json(lookup, sample)
        nier_text.dialogue_lines.cache_clear()
        assert nier_text.dialogue_lines() == sample
        assert nier_text.dialogue_source() == str(lookup)
    finally:
        nier_text.dialogue_lines.cache_clear()


def test_invalid_wai_offsets_are_rejected(game):
    _, sound = game
    path = sound / STREAM_INDEX
    raw = bytearray(path.read_bytes())
    struct.pack_into('<I', raw, 16 + 32 * 3 + 8, 0x123456)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='不匹配'):
        catalogue(sound, 'ja')


def test_pronunciation_alternate_and_retry_preserve_previous_candidates(game, tmp_path, monkeypatch):
    root, _ = game
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', 100, -100) * 6000)
    settings = Settings(tts_provider='openai_speech', tts_url='http://127.0.0.1:8666/v1')
    monkeypatch.setattr('app.game_voice_api.load_settings', lambda: settings)
    monkeypatch.setattr(GameVoiceProjects, 'source', lambda *_args: source)
    attempted = []

    def synthesize(_pipeline, _id, pending, _total, _settings, _event, _stage):
        attempted.append([(clip['id'], clip['text']) for clip in pending])
        for clip in pending:
            if len(attempted) == 1 and clip['id'] == '2':
                raise RuntimeError('Test synthesis failure')
            Path(clip['output']).write_bytes(source.read_bytes())

    monkeypatch.setattr(Pipeline, 'synthesize', synthesize)

    def finished(client, url):
        for _ in range(100):
            state = client.get(url).json()
            if state['job']['status'] in {'done', 'failed'}:
                return state
            time.sleep(.02)
        pytest.fail('Game voice job did not finish')

    assert spoken_text('请叫2B和12B，还有2B2。', [{'term': '2B', 'spoken': 'two b'}]) == '请叫two b和12B，还有2B2。'
    with TestClient(create_app(tmp_path / 'projects')) as client:
        project = client.post('/api/game-voices', json=dict(title='Test', path=str(root))).json()
        url = '/api/game-voices/' + project['id']
        for index in range(2):
            row = client.put(f'{url}/rows/{index + 1}', json=dict(revision=index, text=f'2B，第{index + 1}句')).json()
            assert row['text'].startswith('2B') and row['spoken_text'].startswith('two b')
        assert client.post(url + '/jobs', json=dict(action='generate', rows=['1', '2'], revision=2)).status_code == 200
        project = finished(client, url)
        assert project['job']['status'] == 'failed' and project['job']['done_rows'] == ['1']
        assert client.post(url + '/retry', json=dict(revision=project['revision'])).status_code == 200
        project = finished(client, url)
        assert project['job']['status'] == 'done' and attempted[1] == [('2', 'two b，第2句')]
        line = client.get(url + '/rows/1').json()
        candidate = next(iter(line['candidates']))
        chosen = client.post(url + '/rows/1/adopt', json=dict(candidate=candidate, revision=line['revision'])).json()
        assert client.post(url + '/jobs', json=dict(action='alternate', rows=['1'], revision=chosen['revision'])).status_code == 200
        assert finished(client, url)['job']['status'] == 'done'
        line = client.get(url + '/rows/1').json()
        assert len(line['candidates']) == 2 and line['selected'] == candidate
        changed = client.put(url + '/pronunciation', json=dict(revision=line['revision'], rules=[dict(term='2B', spoken='two bee')]))
        assert changed.status_code == 200
        line = client.get(url + '/rows/1').json()
        assert not line['selected'] and line['spoken_text'].startswith('two bee') and len(line['candidates']) == 2
