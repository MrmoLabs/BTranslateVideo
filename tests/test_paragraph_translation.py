import json
from threading import Event

import httpx
import pytest

from app.config import Settings
from app.paragraph_translation import aligned_spans, paragraph_batches, uses_paragraph_translation
from app.translation_quality import run_workflow, workflow_batches


def sample_rows():
    phrases = ['Welcome to class.', 'We have 8 questions.', 'Let us start.']
    return [dict(id=index, text=text, zh='', start=2*index, end=2*index+1.5, speaker='host')
            for index, text in enumerate(phrases, 1)]


def mock_completions(monkeypatch, responses):
    calls = []

    async def post(url, payload, cancel, **options):
        content = responses[len(calls)]
        calls.append(payload)
        return httpx.Response(200, request=httpx.Request('POST', url),
                              json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

    monkeypatch.setattr('app.translation._post_cancellable', post)
    return calls


def test_paragraph_mode_translates_before_aligning_and_reuses_validated_cache(monkeypatch,tmp_path):
    paragraph = '欢迎来上课。我们有8个问题。现在开始吧。'
    parts=json.dumps({'parts':['欢迎来上课。','我们有8个问题。','现在开始吧。']})
    calls = mock_completions(monkeypatch,[paragraph,parts,paragraph,parts])
    settings = Settings(translation_layout='paragraph',translation_review='off',translation_compact=False)
    rows = sample_rows()
    args = ('http://localhost',settings,rows,[],'course',Event())
    result = run_workflow(*args,cache=tmp_path)
    assert [value['zh'] for value in result.values()]==['欢迎来上课。','我们有8个问题。','现在开始吧。']
    assert [row['start'] for row in rows]==[2,4,6]
    assert [row['zh'] for row in rows]==['','','']
    first, second = [json.loads(call['messages'][1]['content'].removesuffix('\n/no_think')) for call in calls]
    assert first['source']=='Welcome to class. We have 8 questions. Let us start.'
    assert 'source_cues' not in first
    assert second['translated_paragraph']==paragraph
    assert [cue['id'] for cue in second['source_cues']]==[1,2,3]
    assert run_workflow(*args,cache=tmp_path)==result and len(calls)==2
    custom='像讲师面对听众一样讲话。'
    edited=settings.model_copy(update={'translation_system_prompt':custom})
    assert run_workflow(args[0],edited,*args[2:],cache=tmp_path)==result and len(calls)==4
    assert calls[2]['messages'][0]['content'].startswith(custom)
    assert custom not in calls[3]['messages'][0]['content']


def test_alignment_must_preserve_order_unique_boundaries_and_valid_source_numbers(monkeypatch):
    with pytest.raises(ValueError):
        aligned_spans('开始。好的。结束。',['开始。','好的。好的。','结束。'],3)
    with pytest.raises(ValueError):
        aligned_spans('开始。继续。结束。',['开始。','结束。','继续。'],3)
    with pytest.raises(ValueError):
        aligned_spans('one example',['one ex','ample'],2)
    calls = mock_completions(monkeypatch,[
        '欢迎来上课。我们有9个问题。现在开始吧。',
        json.dumps({'parts':['欢迎来上课。','我们有9个问题。','现在开始吧。']}),
        json.dumps({'translations':[dict(id=1,zh='欢迎来上课。'),dict(id=2,zh='我们有8个问题。'),dict(id=3,zh='现在开始吧。')]}),
    ])
    result = run_workflow('http://localhost',Settings(translation_layout='paragraph',translation_review='off',translation_compact=False),
                          sample_rows(),[],'course',Event())
    assert result[2]['zh']=='我们有8个问题。' and len(calls)==3
    assert 'translations' in calls[-1]['messages'][0]['content']


def test_paragraph_batches_preserve_speaker_changes_gaps_and_saved_lines():
    rows = sample_rows() + [
        dict(id=4,text='My turn.',start=7,end=8,speaker='guest'),
        dict(id=5,text='Later.',start=20,end=21,speaker='guest'),
        dict(id=6,text='Next.',start=21,end=22,speaker='guest'),
    ]
    assert list(paragraph_batches(rows,range(len(rows))))==[[0,1,2],[3],[4,5]]
    assert list(paragraph_batches(rows,[0,2,3,4,5]))==[[0],[2],[3],[4,5]]


def test_online_paragraph_batch_contains_twenty_cues_and_preserves_speaker_boundaries(monkeypatch):
    source = 'Say hello to everyone in the room. ' * 4
    rows = [dict(id=i+1, text=source.strip(), zh='', start=2*i, end=2*i+1.5,
                 speaker='speaker_1' if i<24 else 'speaker_2') for i in range(45)]
    settings = Settings(translation_provider='openai_remote', translation_api_url='https://example.com/v1',
                        translation_layout='paragraph', translation_review='off', translation_compact=False)
    batches = list(workflow_batches(rows, range(len(rows)), settings))
    assert [len(batch) for batch in batches] == [20,4,20,1]
    large = settings.model_copy(update={'translation_remote_batch':64})
    assert [len(batch) for batch in workflow_batches(rows,range(len(rows)),large)] == [24,21]
    assert len(next(workflow_batches(rows,range(len(rows)),Settings(translation_layout='paragraph'))))==8

    parts = ['请向在场的各位问好，和每一位同学打个招呼。'] * 20
    calls = mock_completions(monkeypatch, [''.join(parts), json.dumps({'parts':parts})])
    result = run_workflow(settings.translation_service_url,settings,rows[:20],[],'course',Event())
    assert [item['zh'] for item in result.values()] == parts
    assert len(calls)==2 and all(call['max_tokens']>2200 for call in calls)


def test_paragraph_layout_only_effective_for_locally_managed_gguf():
    assert uses_paragraph_translation(Settings(translation_layout='paragraph'))
    assert not uses_paragraph_translation(Settings(translation_layout='paragraph',translation_provider='bilibili'))
    assert not uses_paragraph_translation(Settings(translation_layout='paragraph',translation_url='http://127.0.0.1:1234/v1'))


@pytest.mark.parametrize('layout', ['paragraph', 'subtitle'])
@pytest.mark.parametrize('capacity_error', [False, True])
def test_online_translation_sends_more_than_64_and_splits_on_capacity(monkeypatch, layout, capacity_error):
    rows = [dict(id=i+1, text='Hello.', zh='', start=2*i, end=2*i+1.5, speaker='host') for i in range(100)]
    settings = Settings(translation_provider='openai_remote', translation_api_url='https://example.com/v1',
                        translation_remote_batch=100, translation_layout=layout,
                        translation_review='off', translation_compact=False)
    batches = list(workflow_batches(rows, range(len(rows)), settings))
    assert batches == [list(range(100))]

    sizes = []

    async def post(url, payload, cancel, **options):
        data = json.loads(payload['messages'][1]['content'].removesuffix('\n/no_think'))
        request = httpx.Request('POST', url)
        ids = data.get('target_ids', [])
        cues = data.get('source_cues', [])
        size = len(ids or cues) if ids or cues else data['source'].count('Hello.')
        sizes.append(size)
        if capacity_error and size == 100:
            if layout == 'subtitle':
                return httpx.Response(400, request=request, json={'error': {'code': 'context_length_exceeded'}})
            return httpx.Response(200, request=request, json={
                'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]})
        if ids:
            content = json.dumps({'translations': [{'id': i, 'zh': '你好。'} for i in ids]})
        elif cues:
            content = json.dumps({'parts': ['你好。'] * size})
        else:
            content = '你好。' * size
        return httpx.Response(200, request=request, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

    monkeypatch.setattr('app.translation._post_cancellable', post)
    result = run_workflow(settings.translation_service_url, settings, rows, [], 'course', Event())
    assert set(result) == set(range(1, 101)) and all(item['zh'] == '你好。' for item in result.values())
    expected = [100] * (2 if layout == 'paragraph' else 1)
    if capacity_error:
        expected = [100] + [50] * (4 if layout == 'paragraph' else 2)
    assert sizes == expected
