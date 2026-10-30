import copy
import json
import re
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.audiobooks import BookStore,concatenate,split_prose
from app.config import Settings
from app.media import Cancelled,wav_duration
from app.server import create_app
from app.store import Store


def tone(path,seconds=3.2,rate=24000):
    path.parent.mkdir(parents=True,exist_ok=True)
    samples=(np.sin(np.arange(round(rate*seconds))*2*np.pi*440/rate)*3000).astype('<i2')
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(rate);stream.writeframes(samples.tobytes())
    return path


def test_prose_splitting_keeps_numbers_quotes_paragraphs_and_all_content():
    text='Dr. Smith met Mr. Jones at 3.14. “Hello!”\n\n他说：“你好！”然后离开。\n'+('没有句号的长文本'*50)
    rows=split_prose(text,max_chars=80)
    assert rows[0]['text']=='Dr. Smith met Mr. Jones at 3.14.'
    assert rows[1]['text']=='“Hello!”'
    assert rows[2]['text']=='他说：“你好！”'
    assert rows[2]['paragraph']==1 and rows[-1]['paragraph']==2
    assert re.sub(r'\s+','',''.join(r['text'] for r in rows))==re.sub(r'\s+','',text)
    assert len({r['id'] for r in rows})==len(rows)
    assert max(len(r['text']) for r in rows)<=81
    assert split_prose('  ')==[]
    with pytest.raises(ValueError):split_prose('x'*250001)


def test_resampling_and_merge_preserve_speed_silence_and_path_quoting(tmp_path):
    first=tone(tmp_path/"a's.wav",.4,22050);second=tone(tmp_path/'b.wav',.6,16000)
    output=tmp_path/'merged.wav';settings=Settings(audio_workers=2)
    concatenate([first,second],output,.25,settings,tmp_path/'process.log',threading.Event())
    assert abs(wav_duration(output)-1.25)<.002
    with wave.open(str(output),'rb') as wav:
        assert wav.getframerate()==24000
        wav.setpos(round(.45*24000));assert wav.readframes(100)==b'\0\0'*100
    assert not list(tmp_path.glob('concat-*.txt'))
    event=threading.Event();event.set()
    with pytest.raises(Cancelled):concatenate([first],tmp_path/'cancelled.wav',0,settings,tmp_path/'process.log',event)
    assert not (tmp_path/'cancelled.wav').exists()


def fixture(app,tmp_path):
    sample=tone(tmp_path/'reference.wav')
    voice=app.state.narration.voices.save('Narrator','Authored test tone',sample)
    book=app.state.books.create('Acceptance novel');chapter=app.state.books.add_chapter(book['id'],'Chapter one')
    rows=split_prose('第一句。第二句。')
    chapter=app.state.books.save(book['id'],chapter['id'],dict(title=chapter['title'],text='第一句。第二句。',sentences=rows,voice_id=voice['id'],language='zh',gap=.2,revision=0))
    model=tmp_path/'model';model.mkdir();return book['id'],chapter['id'],voice,Settings(index_model=str(model))


def wait(narration,book):
    deadline=time.monotonic()+15
    while narration.busy(book) and time.monotonic()<deadline:time.sleep(.02)
    assert not narration.busy(book)


def test_narration_resume_cache_edit_invalidation_and_ordered_exports(tmp_path,monkeypatch):
    calls=[];cancel_once=[True]
    def synthesize(adapter,cid,pending,total,settings,event,stage):
        calls.append(len(pending))
        for index,clip in enumerate(pending):
            tone(Path(clip['output']),.3 if index==0 else .5,22050)
            adapter.store.update(cid,tts_runtime={'phase':'正在合成','completed':index+1,'total':len(pending)})
            if cancel_once[0]:cancel_once[0]=False;raise Cancelled('cancelled for acceptance')
    monkeypatch.setattr('app.audiobooks.Pipeline.synthesize',synthesize)
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        book,cid,voice,settings=fixture(app,tmp_path);service=app.state.narration;store=app.state.books
        service.submit(book,[cid],settings);wait(service,book)
        assert store.book(book)['job']['status']=='cancelled'
        assert len(store.chapter(book,cid)['audios'])==1
        service.submit(book,[cid],settings);wait(service,book)
        chapter=store.chapter(book,cid)
        assert store.book(book)['job']['status']=='done',store.book(book)['job']
        assert len(chapter['audios'])==2 and set(chapter['outputs'])=={'mp3','wav'}
        assert abs(wav_duration(store.folder(book,cid)/chapter['outputs']['wav'])-.8)<.005
        assert calls==[2,1]
        service.submit(book,[cid],settings);wait(service,book);assert calls[-1]==0
        before=copy.deepcopy(chapter);chapter['sentences'][1]['text']='第二句修改后。'
        changed=store.save(book,cid,{**chapter,'revision':chapter['revision']})
        assert len(changed['audios'])==1 and changed['outputs']=={}
        with pytest.raises(Exception):service.submit(book,[cid],settings,kind='export')
        service.submit(book,[cid],settings);wait(service,book);assert calls[-1]==1
        chapter=store.chapter(book,cid)
        service.submit(book,[cid],settings,kind='export',format='wav');wait(service,book)
        name=store.book(book)['outputs']['wav']
        assert client.get(f'/api/audiobooks/{book}/files/{name}',headers={'Range':'bytes=0-99'}).status_code==206
        assert client.get(f'/api/audiobooks/{book}/chapters/{cid}/files/tts-'+('a'*24)+'.wav').status_code==404
        assert client.put(f'/api/audiobooks/{book}/chapters/{cid}',json={**before,'revision':0}).status_code==409
        assert client.get('/api/audiobooks/invalid').status_code==400
        assert client.post('/api/audiobooks',json={'title':'cross site'},headers={'Origin':'https://example.invalid'}).status_code==403
        service.pipeline.events[service.key(book)]=threading.Event()
        try:
            assert client.put(f'/api/audiobooks/{book}/chapters/{cid}',json=chapter).status_code==409
            assert client.post(f'/api/audiobooks/{book}/archive').status_code==409
        finally:service.pipeline.events.pop(service.key(book))
        assert client.post(f'/api/audiobooks/{book}/archive').json()['archived'] is True
        assert client.get('/api/audiobooks').json()==[]
        assert client.get('/api/audiobooks?archived=true').json()[0]['id']==book
        client.post(f'/api/audiobooks/{book}/archive?archived=false')
        assert client.get(f'/api/audiobooks/{book}').status_code==200


def test_recovery_and_missing_voice_rejects_before_queueing(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        book,cid,voice,settings=fixture(app,tmp_path)
        app.state.books.update(book,cid,status='running')
        app.state.books.update_book(book,job={'status':'running'})
        app.state.books.recover()
        assert app.state.books.chapter(book,cid)['status']=='interrupted'
        chapter=app.state.books.chapter(book,cid)
        chapter['voice_id']=uuid.uuid4().hex
        app.state.books.save(book,cid,chapter)
        response=client.post(f'/api/audiobooks/{book}/generate',json={'chapter_ids':[cid]})
        assert response.status_code==400 and not app.state.narration.busy(book)
        assert client.get(f'/api/audiobooks/{book}/files/book-'+('a'*24)+'.wav').status_code==404
        assert client.put(f'/api/audiobooks/{book}/chapters/{cid}',json={**chapter,'sentences':[{'id':uuid.uuid4().hex,'text':{}}]}).status_code==422


def test_large_chapter_split_and_save_do_not_drop_content(tmp_path):
    text='清晨的阳光照进山谷，远处传来鸟鸣。'*1800
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        rows=client.post('/api/audiobooks/split',json={'text':text}).json()['sentences']
        assert len(rows)==1800 and ''.join(row['text'] for row in rows)==text
        book=client.post('/api/audiobooks',json={'title':'Long prose'}).json()
        chapter=client.post(f"/api/audiobooks/{book['id']}/chapters",json={'title':'Long chapter'}).json()
        result=client.put(f"/api/audiobooks/{book['id']}/chapters/{chapter['id']}",json={**chapter,'text':text,'sentences':rows})
        assert result.status_code==200 and len(result.json()['sentences'])==1800
        assert client.post('/api/audiobooks/split',json={'text':'x'*250001}).status_code==422
