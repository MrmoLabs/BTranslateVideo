"""Bounded, explicit local microphone / WASAPI-loopback reference recording."""
from __future__ import annotations

import math
import os
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np
from fastapi import HTTPException


class Recorder:
    def __init__(self,root,provider=None):
        self.root=Path(root);self.provider=provider;self.lock=threading.RLock();self.sessions={};self.active=None

    def module(self):
        if self.provider:return self.provider
        if os.name!='nt':raise ValueError('本机录音目前使用 Windows WASAPI；其他系统可上传录音文件')
        try:import pyaudiowpatch
        except ImportError as exc:raise ValueError('录音组件尚未安装，请运行 setup.ps1 -SkipModels') from exc
        return pyaudiowpatch

    def devices(self):
        try:
            module=self.module()
            with module.PyAudio() as audio:
                host=audio.get_host_api_info_by_type(module.paWASAPI)
                try:output=audio.get_default_wasapi_loopback()
                except OSError:output=None
                microphones=[];systems=[]
                for info in audio.get_device_info_generator():
                    if info['hostApi']!=host['index'] or info.get('maxInputChannels',0)<1:continue
                    row=dict(id=int(info['index']),name=info['name'],default=int(info['index'])==int((output or {}).get('index',-1) if info.get('isLoopbackDevice') else host['defaultInputDevice']))
                    (systems if info.get('isLoopbackDevice') else microphones).append(row)
                return dict(available=True,microphone=microphones,system=systems,error='')
        except Exception as exc:return dict(available=False,microphone=[],system=[],error=str(exc))

    def start(self,kind,device=None):
        if kind not in {'microphone','system'}:raise ValueError('无效录音来源')
        devices=self.devices()
        if not devices['available']:raise ValueError(devices['error'])
        choices=devices[kind]
        if not choices:raise ValueError('没有可用的录音设备，请检查 Windows 声音与麦克风权限')
        chosen=next((d for d in choices if d['id']==device),None) if device is not None else next((d for d in choices if d['default']),choices[0])
        if not chosen:raise ValueError('录音设备已变化，请刷新设备列表')
        with self.lock:
            if self.active and self.sessions[self.active]['status'] in {'starting','recording','stopping'}:raise HTTPException(409,'已有录音正在进行，请先停止或取消')
            token=uuid.uuid4().hex;folder=self.root/token;folder.mkdir(parents=True,exist_ok=True)
            session=dict(id=token,kind=kind,device=chosen['name'],device_id=chosen['id'],status='starting',duration=0.,level=0.,error='',stop=threading.Event(),discard=False,created=time.monotonic(),path=folder/'recording.wav')
            self.sessions[token]=session;self.active=token
            thread=threading.Thread(target=self.capture,args=(session,),daemon=True,name='reference-recording');session['thread']=thread;thread.start()
            return self.status(token)

    def status(self,token):
        with self.lock:
            if token not in self.sessions:raise FileNotFoundError(token)
            session=self.sessions[token]
            return {key:session[key] for key in ('id','kind','device','status','duration','level','error')}

    def capture(self,session):
        output=session['path'];pending=output.with_suffix('.pending.wav')
        try:
            module=self.module()
            with module.PyAudio() as audio:
                info=audio.get_device_info_by_index(session['device_id'])
                if info['name']!=session['device'] or bool(info.get('isLoopbackDevice'))!=(session['kind']=='system'):raise ValueError('录音设备已变化，请重新选择设备')
                rate=int(info['defaultSampleRate']);channels=min(2,int(info['maxInputChannels']))
                if channels<1:raise ValueError('这个设备没有可录制的音轨')
                frames=0;peak=0.;chunk=max(128,int(rate/20));maximum=round(rate*29)
                with wave.open(str(pending),'wb') as wav:
                    wav.setnchannels(channels);wav.setsampwidth(2);wav.setframerate(rate)
                    with audio.open(format=module.paInt16,channels=channels,rate=rate,input=True,input_device_index=session['device_id'],frames_per_buffer=chunk) as stream:
                        with self.lock:session['status']='recording'
                        start=time.monotonic()
                        while not session['stop'].is_set() and frames<maximum and time.monotonic()-start<29:
                            available=stream.get_read_available() if hasattr(stream,'get_read_available') else chunk
                            if available<=0:
                                with self.lock:session['duration']=round(min(29,time.monotonic()-start),2)
                                session['stop'].wait(.02);continue
                            data=stream.read(min(chunk,available,maximum-frames),exception_on_overflow=False)
                            if not data:raise ValueError('录音设备未返回音频')
                            samples=np.frombuffer(data,dtype='<i2');value=float(np.max(np.abs(samples.astype(np.float32))))/32768 if samples.size else 0.
                            peak=max(peak,value);wav.writeframesraw(data);frames+=len(data)//(2*channels)
                            with self.lock:session.update(duration=round(frames/rate,2),level=min(1.,value))
            if session['discard']:session['status']='cancelled';return
            if frames/rate<3:raise ValueError('录音不足 3 秒，请重新录制')
            if peak<.0005:raise ValueError('未录到有效声音，请检查录音设备或系统播放音量')
            os.replace(pending,output)
            with self.lock:session.update(status='ready',level=0.)
        except Exception as exc:
            with self.lock:session.update(status='cancelled' if session['discard'] else 'failed',error='' if session['discard'] else str(exc) or type(exc).__name__,level=0.)
        finally:
            pending.unlink(missing_ok=True)
            if session['discard']:
                try:output.unlink(missing_ok=True)
                except OSError:pass
            with self.lock:
                if self.active==session['id']:self.active=None

    def stop(self,token,discard=False):
        with self.lock:
            if token not in self.sessions:raise FileNotFoundError(token)
            session=self.sessions[token];session['discard']|=discard;session['stop'].set()
            if session['status'] in {'starting','recording'}:session['status']='stopping'
            if discard and session['status'] in {'ready','failed','cancelled'}:
                try:session['path'].unlink(missing_ok=True)
                except OSError:pass
                session['status']='cancelled'
            return self.status(token)

    def audio(self,token):
        self.status(token);session=self.sessions[token]
        if session['status']!='ready' or not session['path'].is_file():raise HTTPException(409,'录音尚未准备好')
        return session['path']

    def close(self):
        for token in list(self.sessions):
            self.stop(token,discard=True)
        for session in self.sessions.values():session['thread'].join(timeout=2)
