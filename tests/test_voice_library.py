import io
import threading
import wave
from pathlib import Path

from fastapi.testclient import TestClient
from app.server import create_app


def recording(value=0, seconds=3.2):
    output=io.BytesIO()
    with wave.open(output,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(24000)
        stream.writeframes(int(value).to_bytes(2,'little',signed=True)*int(24000*seconds))
    return output.getvalue()


def upload(client,name='Narrator',value=0,seconds=3.2):
    return client.post('/api/voices',data={'name':name,'description':'Reusable narrator'},files={'audio':('voice.wav',recording(value,seconds),'audio/wav')})


def project(app):
    store=app.state.store;p=store.create('sample','en','course')
    store.update(p['id'],segments=[dict(id=1,start=0,end=5,text='hello',zh='你好',speaker='speaker_1')],media={'duration':5},outputs={'video':'result.mkv'})
    (store.folder(p['id'])/'result.mkv').write_bytes(b'previous output')
    return p['id']


def test_voice_crud_audio_ranges_and_archiving(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        result=upload(client);assert result.status_code==200
        voice=result.json();assert 3<=voice['duration']<=4
        assert client.get('/api/voices').json()[0]['id']==voice['id']
        assert client.get(f"/api/voices/{voice['id']}/audio",headers={'Range':'bytes=0-99'}).status_code==206
        changed=client.put(f"/api/voices/{voice['id']}",data={'name':'Renamed','description':'New description'})
        assert changed.status_code==200 and changed.json()['audio']==voice['audio']
        assert client.post(f"/api/voices/{voice['id']}/archive").status_code==200
        assert client.get('/api/voices').json()==[]
        assert client.get('/api/voices?archived=true').json()[0]['name']=='Renamed'
        assert client.post(f"/api/voices/{voice['id']}/archive?archived=false").json()['archived'] is False
        assert upload(client,seconds=1).status_code==400
        assert client.put('/api/voices/not-an-id',data={'name':'bad'}).status_code==400
        assert client.post('/api/voices',data={'name':'bad'},files={'audio':('x.wav',recording(),'audio/wav')},headers={'Origin':'https://example.invalid'}).status_code==403


def test_project_voice_snapshots_survive_preset_updates_and_archiving(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        voice=upload(client,value=100).json();first=project(app);second=project(app)
        old_audio=[]
        for identifier in [first,second]:
            response=client.put(f'/api/projects/{identifier}/voice/speaker_1',json={'voice_id':voice['id']})
            assert response.status_code==200
            data=response.json();name=data['references']['speaker_1']
            old_audio.append(client.get(f'/api/projects/{identifier}/files/{name}').content)
            assert data['outputs']=={} and data['voice_assignments']['speaker_1']['name']=='Narrator'
            assert list((app.state.store.folder(identifier)/'history').glob('*/result.mkv'))
        assert old_audio[0]==old_audio[1]
        updated=client.put(f"/api/voices/{voice['id']}",data={'name':'New voice'},files={'audio':('voice.wav',recording(500),'audio/wav')})
        assert updated.status_code==200
        assert client.get(f"/api/voices/{voice['id']}/audio").content!=old_audio[0]
        client.post(f"/api/voices/{voice['id']}/archive")
        for identifier in [first,second]:
            data=client.get(f'/api/projects/{identifier}').json();name=data['references']['speaker_1']
            assert client.get(f'/api/projects/{identifier}/files/{name}').content==old_audio[0]
            assert client.put(f'/api/projects/{identifier}/voice/speaker_1',json={'voice_id':voice['id']}).status_code==400
        client.post(f"/api/voices/{voice['id']}/archive?archived=false")
        app.state.pipeline.events[first]=threading.Event()
        try:assert client.put(f'/api/projects/{first}/voice/speaker_1',json={'voice_id':voice['id']}).status_code==409
        finally:app.state.pipeline.events.pop(first)
        assert client.put(f'/api/projects/{first}/voice/unknown',json={'voice_id':voice['id']}).status_code==400


def test_existing_project_reference_can_be_saved_as_reusable_preset(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        identifier=project(app);folder=app.state.store.folder(identifier)
        (folder/'reference.wav').write_bytes(recording(100))
        app.state.store.update(identifier,references={'speaker_1':'reference.wav'})
        result=client.post('/api/voices/from-project',json={'project_id':identifier,'speaker':'speaker_1','name':'Existing speaker'})
        assert result.status_code==200
        assert client.get('/api/voices').json()[0]['name']=='Existing speaker'
        (folder/'reference.wav').write_bytes(recording(200))
        assert client.get(f"/api/voices/{result.json()['id']}/audio").content!=recording(200)


def test_log_view_is_bounded_and_download_keeps_complete_utf8_output(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        identifier=project(app);folder=app.state.store.folder(identifier)
        text=''.join(f'诊断记录 {i}\n' for i in range(5000))
        (folder/'process.log').write_text(text,encoding='utf-8')
        app.state.store.log(identifier,'测试完成')
        result=client.get(f'/api/projects/{identifier}/logs?lines=80').json()
        assert result['truncated'] and len(result['diagnostic'].splitlines())==80
        assert result['diagnostic'].splitlines()[-1]=='诊断记录 4999'
        assert result['activity'][-1]['message']=='测试完成'
        assert len(client.get(f'/api/projects/{identifier}/logs?lines=10000').json()['diagnostic'].splitlines())==3000
        download=client.get(f'/api/projects/{identifier}/logs/download')
        assert download.status_code==200 and 'attachment' in download.headers['content-disposition']
        assert download.content.endswith((folder/'process.log').read_bytes())
        assert client.get('/api/projects/invalid/logs').status_code==400
