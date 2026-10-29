from contextlib import contextmanager
import json
from pathlib import Path
import threading

from app.config import Settings
from app.export_media import original_background
from app.pipeline import Pipeline
from app.store import Store


def test_translation_releases_warm_tts_before_opening_translator(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');project=store.create('test','fr','course')
    row=dict(id=1,start=0,end=3,text='Bonjour.',zh='',speaker='speaker_1')
    store.update(project['id'],media={'duration':5},segments=[row])
    pipeline=Pipeline(store);calls=[]
    monkeypatch.setattr(pipeline.tts,'release',lambda reason:calls.append('release'))
    @contextmanager
    def translator(*_):
        assert calls==['release'];calls.append('translator');yield 'unused'
    monkeypatch.setattr('app.pipeline.local_translator',translator)
    monkeypatch.setattr('app.pipeline.translate_batch',lambda *_a,**_kw:{1:'你好。'})
    try:
        pipeline.translate(project['id'],Settings(),threading.Event())
        assert calls==['release','translator']
    finally:pipeline.close()


def test_background_cache_keeps_tts_but_new_separation_releases_it(tmp_path,monkeypatch):
    store=Store(tmp_path/'projects');project=store.create('test','en','course');folder=store.folder(project['id'])
    (folder/'cache').mkdir();(folder/'source.media').write_bytes(b'input')
    store.update(project['id'],media={'duration':5})
    model=tmp_path/'model';model.mkdir()
    for name in ['manifest.json','config.json','htdemucs.safetensors']:(model/name).write_text('{}',encoding='utf-8')
    (model/'manifest.json').write_text(json.dumps({'weights_sha256':'test'}),encoding='utf-8')
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr('app.export_media.separation_python',lambda:python)
    monkeypatch.setattr('app.export_media.probe',lambda *_:{'duration':5})
    calls=[]
    def worker(args,*_a,**_kw):
        assert calls==['release'];calls.append('worker')
        request=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
        Path(request['output']).write_bytes(b'background')
    monkeypatch.setattr('app.export_media.run',worker)
    def release():calls.append('release')
    arguments=(store,project['id'],Settings(separation_model=str(model)),threading.Event(),lambda *_:None,{})
    first=original_background(*arguments,before_gpu=release)
    second=original_background(*arguments,before_gpu=release)
    assert first==second and calls==['release','worker']
