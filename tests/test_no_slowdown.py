import json,shutil,threading,wave
from pathlib import Path
import numpy as np
import pytest
from app.config import Settings
from app.media import fit_clip,wav_duration,mix_timeline
from app.pipeline import Pipeline
from app.store import Store

def voice(path,seconds=.8,rate=24000):
    with wave.open(str(path),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate)
        f.writeframes((np.sin(np.arange(round(seconds*rate))*.09)*3000).astype('<i2').tobytes())

@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_short_22050_voice_resamples_without_tempo_or_padding(tmp_path,monkeypatch):
    from app import media
    original=media.ffmpeg;calls=[]
    def run(settings,args,*rest):calls.append(args);return original(settings,args,*rest)
    monkeypatch.setattr(media,'ffmpeg',run)
    source,target=tmp_path/'raw.wav',tmp_path/'fit.wav';voice(source,rate=22050)
    assert fit_clip(source,target,5,Settings(),tmp_path/'ffmpeg.log',threading.Event())==1
    assert abs(wav_duration(source)-wav_duration(target))<=1/24000
    assert all('atempo' not in str(arg) and 'apad' not in str(arg) for arg in calls[0])

def test_short_voice_ends_naturally_and_rest_of_window_is_silent(tmp_path):
    source,target=tmp_path/'voice.wav',tmp_path/'timeline.wav';voice(source)
    mix_timeline([(1,source)],5,target,threading.Event())
    with wave.open(str(target),'rb') as f:data=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2')
    assert np.any(data[24000:43200]) and not np.any(data[43200:])

def test_slowed_or_truncated_short_voice_cache_is_rebuilt_without_tts(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');project=store.create('short','en','course');folder=store.folder(project['id']);(folder/'cache').mkdir();(folder/'reference.wav').write_bytes(b'reference')
    model=tmp_path/'model';model.mkdir();(model/'config.yaml').write_text('model: test',encoding='utf-8')
    settings=Settings(index_model=str(model));store.update(project['id'],segments=[dict(id=1,start=0,end=4,text='Hello',zh='你好',speaker='speaker_1')],media={'duration':5},references={'speaker_1':'reference.wav'})
    monkeypatch.setattr('app.pipeline.index_python',lambda _:Path(__file__))
    synthesized=[]
    def tts(args,*_a,**_kw):
        r=json.loads(Path(args[-1]).read_text(encoding='utf-8'));synthesized.append(True)
        for c in r['clips']:voice(c['output'])
    monkeypatch.setattr('app.pipeline.run',tts)
    pipeline=Pipeline(store)
    try:
        pipeline.dub(project['id'],settings,threading.Event())
        fitted=next((folder/'cache').glob('fit-1-*.wav'));raw=next((folder/'cache').glob('tts-*.wav'))
        for invalid_duration in [2,.4]:
            voice(fitted,seconds=invalid_duration)
            pipeline.dub(project['id'],settings,threading.Event())
            assert fitted.read_bytes()==raw.read_bytes()
        assert len(synthesized)==1
    finally:pipeline.close()
