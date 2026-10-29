import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.languages import SOURCE_LANGUAGES
from app.server import create_app


def test_all_exposed_languages_can_be_imported_and_invalid_code_is_rejected(tmp_path,monkeypatch):
    source=tmp_path/'input.mp4';source.write_bytes(b'authored test data')
    monkeypatch.setattr('app.server.probe',lambda *_:dict(duration=8,video=True,audio=True))
    with TestClient(create_app(tmp_path/'projects')) as client:
        assert client.get('/api/languages').json()==SOURCE_LANGUAGES
        for code in SOURCE_LANGUAGES:
            response=client.post('/api/projects/import',json=dict(path=str(source),language=code))
            assert response.status_code==200
            assert response.json()['language']==code
        assert client.post('/api/projects/import',json=dict(path=str(source),language='invalid')).status_code==400


def test_english_catalog_preserves_template_slots_and_has_all_static_strings():
    from html.parser import HTMLParser
    root=Path(__file__).resolve().parents[1]
    catalog=json.loads((root/'app/static/locales/en.json').read_text(encoding='utf-8'))
    for key,value in catalog.items():
        assert value
        assert set(re.findall(r'\{\d+\}',key))==set(re.findall(r'\{\d+\}',value))
    class Keys(HTMLParser):
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if 'data-i18n' in attrs:assert attrs['data-i18n'] in catalog
            for value in json.loads(attrs.get('data-i18n-attrs','{}')).values():assert value in catalog
    Keys().feed((root/'app/static/index.html').read_text(encoding='utf-8'))
