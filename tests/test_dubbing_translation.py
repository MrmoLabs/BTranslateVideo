from contextlib import contextmanager
import json
from threading import Event

import httpx
import pytest

from app.audio_alignment import conflict_report
from app.config import Settings
from app.dubbing_translation import measured_conflict, shorten_to_alignment
from app.translation_quality import _request


def test_measured_shortening_uses_homura_then_review(tmp_path, monkeypatch):
    row = dict(id=4, text='最多重试3次。', zh='The operation can be retried up to a maximum of 3 times.', start=0, end=3)
    project = dict(name='Test',language='zh',target_language='en',mode='course',edit_revision=2,
                   alignment_report=dict(status='blocked',revision=2,conflicts=[dict(id=4,voice_seconds=6,window_seconds=3)]))
    model = tmp_path/'model.gguf'
    model.touch()
    monkeypatch.setattr('app.dubbing_translation.resolve',lambda _:model)
    models=[]

    @contextmanager
    def translator(settings,_log,_cancel):
        models.append(settings.translation_model_id)
        yield 'http://testserver'

    async def completion(_url,payload,_cancel):
        assert payload['model']=='index-homura'
        assert '音节' in payload['messages'][0]['content']
        return httpx.Response(200,request=httpx.Request('POST','http://testserver'),
                              json={'choices':[{'message':{'content':'Retry up to 3 times.'}}]})

    def review(_url,_settings,_rows,_context,_mode,_cancel,task,drafts,_topic,*,speech_limits):
        assert task=='review' and drafts=={4:'Retry up to 3 times.'}
        assert 2 < speech_limits[4] < 2.5
        return {4:dict(zh=drafts[4],checks=[])}

    monkeypatch.setattr('app.dubbing_translation.local_translator',translator)
    monkeypatch.setattr('app.dubbing_translation._post_cancellable',completion)
    monkeypatch.setattr('app.dubbing_translation._request',review)
    result=shorten_to_alignment(project,row,[],Settings(translation_name='reviewer'),tmp_path/'log',Event())
    assert result['after']=='Retry up to 3 times.'
    assert models==['index-homura','reviewer']
    assert measured_conflict({**project,'edit_revision':3},row) is None


def test_conflict_uses_neighbour_occupancy_and_preserves_direct_conflict():
    rows = [dict(id=1, start=0, end=2), dict(id=2, start=2, end=4),
            dict(id=3, start=4, end=6), dict(id=4, start=6, end=8)]
    settings = Settings(max_speed=1.25, max_lead=0, max_lag=0)
    conflicts = conflict_report(rows, [2.6, 2.35, 1, 1], 8, settings)
    project = dict(alignment_report=dict(status='blocked', revision=0, conflicts=conflicts))
    first = measured_conflict(project, rows[0])
    assert first['reason'] == 'sequence_pressure'
    assert first['target_voice_seconds'] == pytest.approx(2.443, abs=.002)
    second = measured_conflict(project, rows[1])
    assert second['reason'] == 'sequence_pressure'
    assert second['target_voice_seconds'] < first['target_voice_seconds']


def test_review_can_revise_a_semantically_valid_but_overlong_draft(monkeypatch):
    row = dict(id=1, text='最多重试3次。', start=0, end=2.7)
    draft = 'The operation can be retried up to 3 times.'
    responses = iter([draft, 'Retry up to 3 times.'])
    prompts = []

    async def completion(_url, payload, _cancel):
        prompts.append(payload['messages'])
        content = json.dumps({'translations': [{'id': 1, 'translation': next(responses)}]})
        return httpx.Response(200, request=httpx.Request('POST', 'http://testserver'),
                              json={'choices': [{'message': {'content': content}}]})

    monkeypatch.setattr('app.translation._post_cancellable', completion)
    cancel = Event()
    monkeypatch.setattr(cancel, 'wait', lambda _seconds: None)
    result = _request('http://testserver', Settings(), [row], [], 'course', cancel,
                      'review', {1: draft}, {'target_language': 'en'}, speech_limits={1: 2.1})
    assert result[1]['zh'] == 'Retry up to 3 times.'
    assert 'max_estimated_seconds' in prompts[0][1]['content']
    assert '超过上限' in prompts[1][1]['content']
