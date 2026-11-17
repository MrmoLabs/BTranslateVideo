import io
import json
import math
import struct
import threading
import wave
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.server import create_app
from app.config import Settings
from app.media import ffmpeg

def tone(path,frequency=None):
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,24000,0,'NONE','not compressed'))
        out.writeframes(b''.join(struct.pack('<h',int(1000*math.sin(2*math.pi*(frequency or (220 if i<96000 else 660))*i/24000))) for i in range(192000)))

@pytest.fixture
def source(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        st=app.state.store;p=st.create('source checking','ja','course');id=p['id'];folder=st.folder(id)
        tone(folder/'source.media')
        st.update(id,media={'duration':8,'audio':True},segments=[dict(id=1,start=0,end=2,text='First.',zh='原译文',speaker='speaker_1'),dict(id=17,start=4,end=6,text='Second.',zh='旧译文',speaker='speaker_1')],outputs={'video':'old.mkv'},status='failed',error='previous task')
        yield app,client,st,id,folder

def audio(content):
    with wave.open(io.BytesIO(content),'rb') as stream:
        rate=stream.getframerate();frames=stream.getnframes();values=np.frombuffer(stream.readframes(frames),dtype='<i2')
    peak=float(np.fft.rfftfreq(len(values),1/rate)[np.argmax(np.abs(np.fft.rfft(values.astype(float))))])
    return frames/rate,peak

def test_audio_excerpt_is_the_selected_time_not_the_first_sentence(source):
    app,client,st,id,folder=source;before=(folder/'project.json').read_bytes()
    url=f'/api/projects/{id}/source-audio/'
    first=client.get(url+'1');second=client.get(url+'17')
    assert first.status_code==second.status_code==200
    assert first.headers['content-type'].startswith('audio/wav')
    assert audio(first.content)==pytest.approx((2,220),abs=.1)
    assert audio(second.content)==pytest.approx((2,660),abs=.1)
    assert (folder/'project.json').read_bytes()==before and not app.state.pipeline.busy(id)

def test_excerpt_supports_ranges_reuses_cache_and_recovers_bad_cache(source):
    _,client,_,id,folder=source;url=f'/api/projects/{id}/source-audio/17'
    full=client.get(url);path=next((folder/'cache').glob('source-cue-*.wav'));mtime=path.stat().st_mtime_ns
    part=client.get(url,headers={'Range':'bytes=0-99'})
    assert part.status_code==206 and part.content==full.content[:100] and path.stat().st_mtime_ns==mtime
    path.write_bytes(b'broken RIFF cache')
    repaired=client.get(url)
    assert repaired.status_code==200 and audio(repaired.content)==pytest.approx((2,660),abs=.1)

def test_draft_timing_and_context_do_not_save_or_replace_any_subtitle(source):
    _,client,_,id,folder=source;before=(folder/'project.json').read_bytes();url=f'/api/projects/{id}/source-audio/1'
    draft=client.get(url,params={'start':4.25,'end':4.75})
    assert audio(draft.content)==pytest.approx((.5,660),abs=.1)
    context=client.get(url,params={'start':4.25,'end':4.75,'context':2})
    assert audio(context.content)[0]==pytest.approx(4.5,abs=.02)
    assert context.headers['x-source-start']=='2.25' and context.headers['x-source-end']=='6.75'
    bounded=client.get(url,params={'start':0,'end':1,'context':2})
    assert audio(bounded.content)[0]==pytest.approx(3,abs=.02)
    assert (folder/'project.json').read_bytes()==before

@pytest.mark.parametrize('params',[{'start':-1},{'end':0},{'start':5,'end':4},{'end':9},{'start':'nan'},{'end':'inf'},{'context':3}])
def test_invalid_excerpt_time_cannot_create_audio(source,params):
    _,client,_,id,folder=source;response=client.get(f'/api/projects/{id}/source-audio/1',params=params)
    assert response.status_code in [400,422]
    assert not list(folder.glob('cache/source-cue-*.wav'))

def test_source_change_invalidates_cache(source):
    _,client,_,id,folder=source;url=f'/api/projects/{id}/source-audio/1'
    old=client.get(url);tone(folder/'source.media',440);new=client.get(url)
    assert audio(old.content)[1]==pytest.approx(220,abs=.1) and audio(new.content)[1]==pytest.approx(440,abs=.1)
    assert len(list((folder/'cache').glob('source-cue-*.wav')))==2


def test_waveform_is_bounded_and_uses_the_requested_window_without_saving(source):
    _,client,_,id,folder=source;before=(folder/'project.json').read_bytes()
    response=client.get(f'/api/projects/{id}/source-waveform/17',params={'start':3.5,'end':6.5})
    assert response.status_code==200,response.text
    value=response.json();assert value['start']==3.5 and value['end']==6.5
    assert 1<=len(value['peaks'])<=1000 and all(0<=v<=1 for v in value['peaks']) and max(value['peaks'])>0
    assert (folder/'project.json').read_bytes()==before
    assert client.get(f'/api/projects/{id}/source-waveform/17',params={'end':100}).status_code==400

def test_missing_project_sentence_or_audio_is_reported(source):
    _,client,st,id,_=source
    assert client.get(f'/api/projects/{id}/source-audio/999').status_code==400
    assert client.get('/api/projects/'+'0'*32+'/source-audio/1').status_code==404
    st.update(id,media={'duration':8,'audio':False})
    response=client.get(f'/api/projects/{id}/source-audio/1')
    assert response.status_code==400 and '音轨' in response.json()['detail']

def test_source_matroska_video_is_decoded_to_browser_ready_audio(source):
    _,client,_,id,folder=source
    (folder/'source.media').rename(folder/'tone.wav')
    ffmpeg(Settings(),['-f','lavfi','-i','color=c=green:s=160x90:r=10:d=8','-i',str(folder/'tone.wav'),'-c:v','ffv1','-c:a','pcm_s16le','-shortest','-f','matroska',str(folder/'source.media')],folder/'fixture.log',threading.Event())
    response=client.get(f'/api/projects/{id}/source-audio/17')
    assert response.status_code==200 and audio(response.content)==pytest.approx((2,660),abs=.1)
