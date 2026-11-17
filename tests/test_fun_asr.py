import copy
import json
from pathlib import Path
import sys
import threading
import types
import wave

import pytest

from app.config import Settings
from app.fun_asr import MODEL_FILES, VAD_FILES, cache_signature, language_name, ready, timed_segment, worker_request
from app.model_backends import catalog, check_backend, transcribe

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fun_asr_worker as worker


def result():
    return dict(text='巴适！', timestamps=[dict(token='巴', start_time=.1, end_time=.3),
                                        dict(token='适！', start_time=.3, end_time=.6)])


def settings_with_files(tmp_path):
    settings = Settings(asr_provider='fun_asr', fun_asr_model=str(tmp_path / 'nano'),
                        fun_asr_vad_model=str(tmp_path / 'vad'), fun_asr_python=str(tmp_path / 'python.exe'))
    Path(settings.fun_asr_python).touch()
    for folder, files in [(settings.fun_asr_model, MODEL_FILES), (settings.fun_asr_vad_model, VAD_FILES)]:
        for name in files:
            path = Path(folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
    return settings


def test_native_timing_and_cjk_text_are_preserved():
    segment = timed_segment(result(), 8, 2, 1)
    assert segment['text'] == '巴适！' and segment['start'] == 8.1 and segment['end'] == 8.6
    assert segment['words'][1] == dict(word='适！', start=8.3, end=8.6)
    from app.segmentation import flatten_words
    assert len(flatten_words([segment], 10)) == 2


def test_standalone_ctc_punctuation_survives_semantic_segmentation():
    from app.segmentation import flatten_words, parse_sentences
    native = dict(text='巴适！安逸。', timestamps=[
        dict(token=token,start_time=i*.1,end_time=(i+1)*.1)
        for i,token in enumerate('巴适！安逸。')])
    segment = timed_segment(native, 0, 1, 1)
    words = flatten_words([segment], 1)
    assert [w['word'] for w in words] == ['巴','适！','安','逸。']
    assert words[1]['end'] == .2 and words[-1]['end'] == .5
    cuts = parse_sentences(json.dumps(dict(sentences=['巴适！','安逸。']),ensure_ascii=False), words)
    assert [cut['end'] for cut in cuts] == [2,4]


@pytest.mark.parametrize('bad', [
    dict(text='巴适'), dict(text='巴适', ctc_timestamps=result()['timestamps']),
    dict(text='巴适得板', timestamps=result()['timestamps']),
    dict(text='巴适', timestamps=[dict(token='巴适', start_time=float('nan'), end_time=.4)]),
    dict(text='巴适', timestamps=[dict(token='巴适', start_time=.1, end_time=20)]),
    dict(text='巴适', timestamps=[dict(token='巴', start_time=.5, end_time=.6), dict(token='适', start_time=.2, end_time=.3)]),
])
def test_incomplete_or_invalid_timing_is_rejected(bad):
    with pytest.raises(ValueError):
        timed_segment(bad, 0, 2, 1)


def test_readiness_and_cache_track_local_files_and_hotwords(tmp_path):
    settings = settings_with_files(tmp_path)
    assert ready(settings) and check_backend('asr', settings)['level'] == 'files'
    before = cache_signature(settings)
    assert json.loads(json.dumps(before)) == before
    (Path(settings.fun_asr_model) / 'model.pt').write_bytes(b'replaced weights')
    assert before != cache_signature(settings)
    settings.fun_asr_hotwords = '重庆，成都\n巴适'
    assert worker_request(settings)['hotwords'] == ['重庆', '成都', '巴适']
    assert catalog()['asr'][1]['id'] == 'fun_asr'
    Path(settings.fun_asr_python).unlink()
    assert not ready(settings) and not check_backend('asr', settings)['ok']
    with pytest.raises(ValueError, match='prepare_fun_asr'):
        worker_request(settings)


def test_dispatch_uses_isolated_worker_and_preserves_whisper_settings(tmp_path, monkeypatch):
    settings = settings_with_files(tmp_path)
    calls = []
    monkeypatch.setattr('app.model_backends.run', lambda args, *a, **kw: calls.append(args))
    transcribe(settings, tmp_path / 'audio.wav', tmp_path / 'out.json', 'zh', 'course', tmp_path / 'log', threading.Event(), {})
    assert calls[0][0] == Path(settings.fun_asr_python)
    assert calls[0][1].name == 'fun_asr_worker.py'
    request = json.loads(Path(calls[0][-1]).read_text(encoding='utf-8'))
    assert request['model'] == settings.fun_asr_model and settings.asr_model == Settings().asr_model
    with pytest.raises(ValueError, match='原声语言'):
        transcribe(settings, tmp_path / 'a', tmp_path / 'out', 'es', '', tmp_path / 'log', threading.Event(), {})
    assert language_name('zh') == '中文' and language_name('') is None


def test_vad_windows_bound_long_audio_and_keep_gaps():
    assert list(worker.speech_windows([[1000, 64000], [65000, 66000]], 66 * 16000)) == [
        (16000, 31 * 16000), (31 * 16000, 61 * 16000), (61 * 16000, 64 * 16000), (65 * 16000, 66 * 16000)]
    with pytest.raises(ValueError):
        list(worker.speech_windows([[1000, 3000], [2000, 4000]], 5 * 16000))


def test_worker_uses_actual_vad_offsets_and_does_not_publish_failed_results(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(from_numpy=lambda value: value))
    audio = tmp_path / 'audio.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 16000 * 4)
    class Vad:
        def generate(self, **kw):
            return [dict(value=[[1000, 2000], [3000, 4000]])]
    class Model:
        bad = False
        calls = []
        def generate(self, **kw):
            self.calls.append(kw)
            return [dict(text='巴适') if self.bad else copy.deepcopy(result())]
    model = Model()
    request = dict(audio=str(audio), output=str(tmp_path / 'out.json'), language='zh', hotwords=['重庆'])
    worker.transcribe(request, (model, Vad()))
    original = Path(request['output']).read_bytes()
    rows = json.loads(original)
    assert rows[0]['start'] == 1.1 and rows[1]['start'] == 3.1
    assert model.calls[0]['language'] == '中文' and model.calls[0]['hotwords'] == ['重庆']
    model.bad = True
    with pytest.raises(ValueError, match='时间戳'):
        worker.transcribe(request, (model, Vad()))
    assert Path(request['output']).read_bytes() == original
