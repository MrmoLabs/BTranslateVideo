import threading
import time
from pathlib import Path
import pytest
import numpy as np
from fastapi import HTTPException
from app.recording import Recorder


class Stream:
    def __init__(self,owner,channels):self.owner,self.channels=owner,channels
    def __enter__(self):self.owner.opened+=1;return self
    def __exit__(self,*_):self.owner.closed+=1
    def read(self,count,**kwargs):
        time.sleep(.001)
        return np.full(count*self.channels,2000 if not self.owner.silent else 0,dtype='<i2').tobytes()
class Audio:
    def __init__(self,owner):self.owner=owner
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def get_host_api_info_by_type(self,_):return {'index':0,'defaultInputDevice':0}
    def get_default_wasapi_loopback(self):return self.get_device_info_by_index(1)
    def get_device_info_generator(self):return [self.get_device_info_by_index(i) for i in range(2)]
    def get_device_info_by_index(self,i):return {'index':i,'name':'Test system' if i else 'Test microphone','hostApi':0,'maxInputChannels':2,'defaultSampleRate':8000,'isLoopbackDevice':bool(i)}
    def open(self,**kwargs):return Stream(self.owner,kwargs['channels'])
class Provider:
    paWASAPI=13;paInt16=8
    def __init__(self,silent=False):self.opened=0;self.closed=0;self.silent=silent
    def PyAudio(self):return Audio(self)


def wait(recorder,token):
    deadline=time.monotonic()+3
    while recorder.status(token)['status'] not in {'ready','failed','cancelled'} and time.monotonic()<deadline:time.sleep(.01)
    return recorder.status(token)


def test_explicit_recording_auto_stops_and_audio_survives_source_manager_close(tmp_path):
    provider=Provider();recorder=Recorder(tmp_path,provider)
    devices=recorder.devices();assert len(devices['microphone'])==len(devices['system'])==1
    assert provider.opened==0
    token=recorder.start('system')['id']
    with pytest.raises(HTTPException):recorder.start('microphone')
    result=wait(recorder,token)
    assert result['status']=='ready' and result['duration']==29
    assert recorder.audio(token).is_file() and provider.opened==provider.closed==1
    recorder.stop(token,discard=True)
    assert not (tmp_path/token/'recording.wav').exists()
    recorder.close()


def test_cancel_and_silence_never_publish_a_reference_file(tmp_path):
    recorder=Recorder(tmp_path,Provider());token=recorder.start('microphone')['id'];recorder.stop(token,discard=True)
    assert wait(recorder,token)['status']=='cancelled'
    assert not (tmp_path/token/'recording.wav').exists()
    recorder.close()
    recorder=Recorder(tmp_path,Provider(silent=True));token=recorder.start('system')['id']
    assert wait(recorder,token)['status']=='failed'
    with pytest.raises(HTTPException):recorder.audio(token)
    assert not list((tmp_path/token).glob('*.wav'))
    recorder.close()
