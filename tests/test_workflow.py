from pathlib import Path
from html.parser import HTMLParser
import json
from scripts.verify_backends import edit_rate,ensure_app_idle

ROOT=Path(__file__).resolve().parents[1]


def test_workflow_controls_have_one_responsible_stage():
    class Page(HTMLParser):
        def __init__(self):super().__init__();self.stage=None;self.locations={};self.targets=[];self.storage_parent=None
        def handle_starttag(self,tag,attrs):
            value=dict(attrs);identifier=value.get('id')
            if identifier and identifier.startswith('panel-'):self.stage=identifier
            if identifier:
                assert identifier not in self.locations,f'duplicate ID: {identifier}'
                self.locations[identifier]=self.stage
            if 'data-target-controls' in value:self.targets.append(self.stage)
    page=Page();page.feed((ROOT/'app/static/index.html').read_text(encoding='utf-8'))
    assert page.locations['run-resegment']=='panel-source'
    assert page.locations['run-translate']==page.locations['run-optimize']==page.locations['chinese-srt']=='panel-subtitles'
    assert page.locations['run-dub']==page.locations['speaker-cards']=='panel-voices'
    assert page.locations['background-mode']==page.locations['background-file']==page.locations['save-audio-options']==page.locations['run-export']=='panel-export'
    assert page.locations['export-source-srt']==page.locations['export-translation-srt']=='panel-subtitles'
    assert page.targets==['panel-subtitles']


def test_authored_fixture_metrics_keep_quality_observations_separate():
    assert edit_rate('one two','one two','en')['rate']==0
    assert edit_rate('one two','one three','en')['rate']==.5
    assert edit_rate('你好世界。','你好，世界！','zh')['rate']==0
    fixture=json.loads((ROOT/'scripts/fixtures/backend_cases.json').read_text(encoding='utf-8'))
    assert {case['language'] for case in fixture['cases']}=={'en','ja'}
    assert all(case['numbers'] and case['review'] for case in fixture['cases'])


def test_backend_acceptance_refuses_active_main_queue(monkeypatch):
    import httpx,pytest
    def handle(request):
        if request.url.path=='/api/tasks':return httpx.Response(200,json=dict(active=1,items=[dict(processing=dict(status='running'))]))
        return httpx.Response(200,json=dict(alive=False))
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(RuntimeError,match='active tasks'):ensure_app_idle()
