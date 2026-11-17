import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.audition_video import make_audition_video
from app.config import Settings
from app.media import Cancelled, extract_audio, ffmpeg, probe, spawn, wav_duration
from app.pipeline import Pipeline
from app.server import create_app


def tone(path,duration,frequency=880):
    frames=np.arange(round(duration*24000));values=(3000*np.sin(2*np.pi*frequency*frames/24000)).astype('<i2')
    with wave.open(str(path),'wb') as output:
        output.setparams((1,2,24000,0,'NONE','not compressed'));output.writeframes(values.tobytes())


@pytest.fixture
def media(tmp_path):
    settings=Settings(max_lead=.75,max_lag=1,preview_encoder='cpu');cancel=threading.Event();(tmp_path/'cache').mkdir()
    # The second half is blue, first half red. An actual original dialogue tone
    # must never leak into the new track, even when previewing unsupported MKV.
    ffmpeg(settings,['-f','lavfi','-i','color=c=red:s=160x90:r=20:d=4','-f','lavfi','-i','color=c=blue:s=160x90:r=20:d=4','-f','lavfi','-i','sine=frequency=220:sample_rate=24000:duration=8','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-map','2:a','-c:v','ffv1','-c:a','pcm_s16le','-f','matroska',str(tmp_path/'source.media')],tmp_path/'fixture.log',cancel)
    rows=[dict(id=1,start=0,end=2,text='First',zh='前一句',speaker='speaker_1'),dict(id=17,start=4,end=6,text='Second',zh='本句草稿',speaker='speaker_1'),dict(id=18,start=7,end=8,text='Last',zh='后一句',speaker='speaker_1')]
    raw=tmp_path/'cache'/'tts-test.wav';tone(raw,.6)
    return dict(media=probe(tmp_path/'source.media',settings),segments=rows),tmp_path,rows[1],raw,settings,cancel


def make(media,**changes):
    project,folder,row,raw,settings,cancel=media
    return make_audition_video(project,folder,{**row,**changes},raw,settings,cancel,lambda *_:None)


def test_correct_picture_dubbed_voice_silent_tail_and_relative_subtitles(media):
    p,folder,row,raw,settings,cancel=media;result=make(media)
    assert result['start']==4 and result['end']==6
    assert result['speed']==1 and result['voice_duration']==pytest.approx(.6)
    info=probe(folder/result['video'],settings);assert info['duration']==pytest.approx(2,abs=.06)
    process=spawn([settings.ffmpeg,'-v','error','-i',folder/result['video'],'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],stdout=-1,stderr=-1)
    data,err=process.communicate(timeout=20);assert process.returncode==0,err
    colors=np.frombuffer(data,dtype=np.uint8).reshape(-1,3).mean(axis=0)
    assert colors[2]>220 and colors[0]<20  # correct second-half frames
    decoded=folder/'decoded.wav';extract_audio(folder/result['video'],decoded,settings,folder/'log',cancel)
    with wave.open(str(decoded),'rb') as stream:values=np.frombuffer(stream.readframes(stream.getnframes()),dtype='<i2').astype(float)
    voice=values[2400:12000];freq=np.fft.rfftfreq(len(voice),1/24000)[np.argmax(abs(np.fft.rfft(voice)))];assert freq==pytest.approx(880,abs=3)
    assert np.sqrt(np.mean(values[24000:42000]**2))<5  # no slow-fill and no original dialogue
    captions=(folder/result['captions']).read_text(encoding='utf-8');assert '00:00:00.000 --> 00:00:00.600' in captions and row['zh'] in captions
    assert wav_duration(raw)==.6 and not list(folder.glob('*.pending.*'))


def test_cache_reuse_and_timing_change_have_distinct_videos(media):
    result=make(media);path=media[1]/result['video'];mtime=path.stat().st_mtime_ns
    assert make(media)==result and path.stat().st_mtime_ns==mtime
    changed=make(media,start=4.5,end=6.5);assert changed['video']!=result['video'] and changed['start']==4.5


def test_long_voice_borrows_only_bounded_gap_and_refuses_to_truncate(media):
    tone(media[3],3.5);result=make(media)
    assert result['voice_duration']>=3.5/media[4].max_speed-.05
    assert 1<result['speed']<=media[4].max_speed
    assert result['start']>=3.25 and result['end']<=7.0001
    tone(media[3],12)
    with pytest.raises(ValueError,match='不会截断'):make(media)


def test_cancel_never_publishes_partial_video(media):
    media[5].set()
    with pytest.raises(Cancelled):make(media)
    assert not list(media[1].glob('audition-video-*')) and not list(media[1].glob('*.pending.*'))


def wait(app,identifier):
    deadline=time.monotonic()+15
    while app.state.pipeline.busy(identifier) and time.monotonic()<deadline:time.sleep(.02)
    assert not app.state.pipeline.busy(identifier)


def test_api_draft_preview_ranges_allowlist_cache_and_failed_preservation(media,monkeypatch,tmp_path):
    project,folder,row,raw,settings,cancel=media
    model=tmp_path/'model';model.mkdir();(model/'config.yaml').write_text('test: true',encoding='utf-8')
    settings=settings.model_copy(update={'index_model':str(model)})
    monkeypatch.setattr('app.server.load_settings',lambda:settings)
    calls=[]
    def synth(self,identifier,pending,*args):
        calls.append(pending)
        for clip in pending:tone(Path(clip['output']),.6)
    monkeypatch.setattr(Pipeline,'synthesize',synth)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;p=store.create('video preview','en','course');identifier=p['id'];target=store.folder(identifier)
        import shutil
        shutil.copyfile(folder/'source.media',target/'source.media');(target/'ref.wav').write_bytes(b'reference');(target/'result.mkv').write_bytes(b'old video')
        store.update(identifier,media=project['media'],segments=project['segments'],references={'speaker_1':'ref.wav'},outputs={'video':'result.mkv'},dub_signature='old-signature')
        before=store.read(identifier);url=f'/api/projects/{identifier}/audition';draft={**row,'zh':'未保存的新台词','emotion':'happy','emotion_strength':'medium'}
        response=client.post(url,json=dict(segment=draft,revision=before['edit_revision'],video=True));assert response.status_code==200
        wait(app,identifier);after=store.read(identifier);assert after['status']=='done',after['error']
        sample=after['audition'];assert sample['segment']==draft and sample['request_id']==response.json()['request_id']
        for key in ['segments','outputs','edit_revision','dub_signature']:assert after[key]==before[key]
        for key,mime in [('video','video/mp4'),('captions','text/vtt')]:
            result=client.get(f"/api/projects/{identifier}/files/{sample[key]}");assert result.status_code==200 and result.headers['content-type'].startswith(mime)
        part=client.get(f"/api/projects/{identifier}/files/{sample['video']}",headers={'Range':'bytes=0-99'});assert part.status_code==206 and len(part.content)==100
        assert client.get(f"/api/projects/{identifier}/files/audition-video-{'0'*24}.mp4").status_code==404
        assert client.get(f'/api/projects/{identifier}/files/project.json').status_code==404
        response=client.post(url,json=dict(segment={**draft,'start':4.25},revision=before['edit_revision'],video=True));assert response.status_code==200
        wait(app,identifier);second=store.read(identifier)['audition'];assert second['video']!=sample['video'] and calls[-1]==[]
        assert client.get(f"/api/projects/{identifier}/files/{sample['video']}").status_code==404
        monkeypatch.setattr('app.audition_video.make_audition_video',lambda *_:(_ for _ in ()).throw(Cancelled('cancelled preview')))
        assert client.post(url,json=dict(segment=draft,revision=before['edit_revision'],video=True)).status_code==200
        wait(app,identifier);cancelled=store.read(identifier);assert cancelled['status']=='cancelled' and cancelled['audition']==second
        monkeypatch.setattr('app.audition_video.make_audition_video',lambda *_:(_ for _ in ()).throw(RuntimeError('encode failed')))
        assert client.post(url,json=dict(segment=draft,revision=before['edit_revision'],video=True)).status_code==200
        wait(app,identifier);failed=store.read(identifier);assert failed['status']=='failed' and failed['audition']==second and failed['segments']==before['segments']
        store.update(identifier,media={**project['media'],'video':False})
        assert client.post(url,json=dict(segment=draft,revision=before['edit_revision'],video=True)).status_code==400
