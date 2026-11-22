from contextlib import contextmanager
from threading import Event

import httpx

from app.config import Settings
from app.dubbing_translation import measured_conflict, shorten_to_alignment


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

    def review(_url,_settings,_rows,_context,_mode,_cancel,task,drafts,_topic):
        assert task=='review' and drafts=={4:'Retry up to 3 times.'}
        return {4:dict(zh=drafts[4],checks=[])}

    monkeypatch.setattr('app.dubbing_translation.local_translator',translator)
    monkeypatch.setattr('app.dubbing_translation._post_cancellable',completion)
    monkeypatch.setattr('app.dubbing_translation._request',review)
    result=shorten_to_alignment(project,row,[],Settings(translation_name='reviewer'),tmp_path/'log',Event())
    assert result['after']=='Retry up to 3 times.'
    assert models==['index-homura','reviewer']
    assert measured_conflict({**project,'edit_revision':3},row) is None
