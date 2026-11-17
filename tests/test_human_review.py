import copy
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.human_review import signature
from app.pipeline import dub_signature
from app.server import create_app
from app.subtitles import validate_segments


def row():
    return dict(id=17,start=.18,end=.54,text='重庆😀',zh='',speaker='speaker_1')


def marked(value=None,status='checked'):
    result=dict(value or row());return {**result,'review_status':status,'review_signature':signature(result)}


def test_review_signature_shared_with_browser_and_invalidated_by_content():
    assert signature(row())=='f0ef0a2dab9e0427444ba0e690846b4984a67f257ba6104dd514d9e0ae0f907b'
    original=marked();assert validate_segments([original])==[original]
    for field,value in [('text','成都'),('zh','Hello'),('start',.19),('end',.55),('speaker','speaker_2')]:
        changed={**original,field:value}
        assert 'review_status' not in validate_segments([changed])[0]
    assert validate_segments([{**original,'emotion':'happy'}])[0]['review_status']=='checked'
    for bad in ['unknown',[],{}]:
        with pytest.raises(ValueError):validate_segments([{**original,'review_status':bad}])


def test_review_only_save_keeps_outputs_word_metadata_and_dub_cache(tmp_path,monkeypatch):
    monkeypatch.setattr('app.pipeline.tts_model_signature',lambda _: 'stable-model')
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;pid=store.create('human review','zh','course')['id'];folder=store.folder(pid)
        original={**row(),'words':[dict(start=.18,end=.54,word='重庆😀')]}
        before=store.update(pid,segments=[original],media={'duration':4},outputs={'video':'result.mkv','audio':'dub.wav'},dub_signature='keep',status='done')
        (folder/'result.mkv').write_bytes(b'previous video')
        cache_before=dub_signature(before,folder,Settings())
        response=client.put(f'/api/projects/{pid}/segments',json=dict(segments=[marked()],revision=before['edit_revision']))
        assert response.status_code==200,response.text
        after=store.read(pid)
        assert after['outputs']==before['outputs'] and after['dub_signature']=='keep' and after['status']=='done'
        assert after['segments'][0]['words']==original['words'] and after['segments'][0]['review_status']=='checked'
        assert dub_signature(after,folder,Settings())==cache_before
        assert not (folder/'history').exists()
        assert client.put(f'/api/projects/{pid}/segments',json=dict(segments=[marked(status='question')],revision=before['edit_revision'])).status_code==409
        # A later pipeline operation can replace text without passing through this endpoint.
        stale=copy.deepcopy(after['segments']);stale[0]['text']='成都'
        store.update(pid,segments=stale)
        assert 'review_status' not in client.get(f'/api/projects/{pid}').json()['segments'][0]


def test_saved_split_keeps_nonsequential_ids_and_invalidates_old_outputs(tmp_path):
    app=create_app(tmp_path/'projects')
    with TestClient(app) as client:
        store=app.state.store;pid=store.create('split','zh','course')['id'];folder=store.folder(pid)
        a={**row(),'end':2,'text':'重庆你好'};b={**row(),'id':2,'start':3,'end':4,'text':'去成都'}
        before=store.update(pid,segments=[a,b],media={'duration':5},outputs={'video':'result.mkv'})
        (folder/'result.mkv').write_bytes(b'previous video')
        split=[{**a,'end':1,'text':'重庆'},{**a,'id':18,'start':1,'text':'你好'},b]
        response=client.put(f'/api/projects/{pid}/segments',json=dict(segments=split,revision=before['edit_revision']))
        assert response.status_code==200,response.text
        assert response.json()['outputs']=={} and [r['id'] for r in response.json()['segments']]==[17,18,2]
        assert list((folder/'history').glob('*/project.json'))
        assert client.get(f'/api/projects/{pid}').json()['segments'][1]['text']=='你好'
