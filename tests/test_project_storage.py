from pathlib import Path
import pytest
from fastapi import HTTPException
from app.store import Store
from app.project_storage import ProjectStorage


def fixture(tmp_path):
    store=Store(tmp_path/'projects');identifier=store.create('storage','en','course')['id'];root=store.folder(identifier)
    for name in ['source.media','reference-speaker_1.wav','result.mp4','cache/tts-good.wav','cache/fit-good.wav','cache/asr.json','cache/sentences-1.json','cache/x.pending.wav','cache/x.invalid-old.bak','cache/subtitle-review-active.json','cache/unknown.wav','history/1/project.json','history/1/old.wav','history/2/project.json','history/2/latest.wav']:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'authored test fixture')
    store.update(identifier,references={'speaker_1':'reference-speaker_1.wav'},outputs={'preview':'result.mp4'},subtitle_review={'id':'active'})
    return store,identifier,ProjectStorage(store)


def test_preview_protects_current_files_and_newest_backup(tmp_path):
    store,identifier,service=fixture(tmp_path)
    report,items=service.inventory(identifier)
    assert report['categories']['analysis']['count']==2
    assert report['categories']['temporary']['count']==2
    assert report['categories']['history']['count']==2
    before=set(store.folder(identifier).rglob('*'))
    plan=service.preview(identifier,['analysis','history','temporary'],0)
    assert set(store.folder(identifier).rglob('*'))==before
    names={item['path'] for item in plan['files']}
    assert names=={'cache/asr.json','cache/sentences-1.json','cache/x.pending.wav','cache/x.invalid-old.bak','history/1/project.json','history/1/old.wav'}
    result=service.clean(identifier,plan['token'],0)
    assert result['removed']==6 and not result['error']
    for name in ['source.media','cache/tts-good.wav','cache/fit-good.wav','cache/subtitle-review-active.json','result.mp4','history/2/latest.wav']:
        assert (store.folder(identifier)/name).exists()
    with pytest.raises(HTTPException):service.clean(identifier,plan['token'],0)


@pytest.mark.parametrize('change',['file','revision','newest','binding'])
def test_stale_preview_deletes_nothing(tmp_path,change):
    store,identifier,service=fixture(tmp_path);root=store.folder(identifier)
    plan=service.preview(identifier,['analysis','history'],0)
    if change=='file':(root/'cache/asr.json').write_bytes(b'changed')
    elif change=='revision':store.update(identifier,segments=[{'id':1}])
    elif change=='newest':
        # A formerly old backup becomes newest after removing the other folder.
        import shutil
        shutil.rmtree(root/'history/2')
    else:store.update(identifier,outputs={'kept':'cache/asr.json'})
    with pytest.raises(HTTPException):service.clean(identifier,plan['token'],0)
    assert (root/'cache/asr.json').exists()
    assert (root/'history/1/old.wav').exists()


def test_cleanup_api_rejects_busy_and_invalid_categories(tmp_path):
    from fastapi.testclient import TestClient
    from app.server import create_app
    app=create_app(tmp_path/'projects');store=app.state.store;identifier=store.create('api','en','course')['id']
    with TestClient(app) as client:
        assert client.get(f'/api/projects/{identifier}/storage').status_code==200
        assert client.post(f'/api/projects/{identifier}/storage/preview',json={'categories':['source'],'revision':0}).status_code==400
        app.state.pipeline.events[identifier]=object()
        assert client.post(f'/api/projects/{identifier}/storage/preview',json={'categories':['analysis'],'revision':0}).status_code==409
        app.state.pipeline.events.clear()


def test_link_outside_project_not_followed(tmp_path):
    store,identifier,service=fixture(tmp_path);outside=tmp_path/'outside';outside.mkdir();(outside/'secret.json').write_text('outside')
    try:(store.folder(identifier)/'cache/link').symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('Creating symlinks requires Windows Developer Mode')
    report,items=service.inventory(identifier)
    assert report['skipped']==1
    assert not any('secret' in item['path'] for files in items.values() for item in files)
