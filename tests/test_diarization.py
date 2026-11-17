import json
import math
from pathlib import Path
import threading
import time
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.diarization import DiarizationOptions, match_segments, validate_turns, overlap_windows, readiness
from app.media import Cancelled, check_cancel
from app.server import create_app


def row(identifier,start,end):
    return dict(id=identifier,start=start,end=end,text=f'Source {identifier}',zh=f'原译文 {identifier}',speaker='speaker_1')


def turns():
    regular=[dict(start=0,end=4,speaker='A'),dict(start=4,end=8,speaker='B'),dict(start=9,end=11,speaker='A'),dict(start=9.2,end=12,speaker='B')]
    exclusive=[dict(start=0,end=4,speaker='A'),dict(start=4,end=8,speaker='B'),dict(start=9,end=12,speaker='B')]
    return regular,exclusive


def test_majority_mapping_flags_silence_multi_speaker_and_overlap():
    regular,exclusive=turns();rows=[row(1,.2,3.7),row(2,4.1,7.7),row(3,7.5,10.5),row(4,8,9),row(5,2,5)]
    result=match_segments(rows,exclusive,regular,{'A':'role_a','B':'role_b'})
    assert [item['after'] for item in result]==['role_a','role_b','role_b',None,'role_a']
    assert [item['needs_review'] for item in result]==[False,False,True,True,True]
    assert result[2]['reasons']==['存在重叠对白']
    assert result[3]['reasons']==['未检测到人声']
    assert result[4]['reasons']==['一句包含多个说话人']
    assert result[4]['agreement']==pytest.approx(.667)
    assert rows[0]['speaker']=='speaker_1'


@pytest.mark.parametrize('options',[dict(device='remote'),dict(num_speakers=0),dict(num_speakers=2,min_speakers=1),dict(min_speakers=4,max_speakers=2),dict(num_speakers=True),dict(num_speakers=2.5)])
def test_speaker_options_are_bounded_and_unambiguous(options):
    with pytest.raises(ValidationError):DiarizationOptions(**options)


@pytest.mark.parametrize('turn',[dict(start=float('nan'),end=1,speaker='A'),dict(start=0,end=float('inf'),speaker='A'),dict(start=-1,end=1,speaker='A'),dict(start=12.01,end=12.05,speaker='A'),dict(start=1,end=0,speaker='A'),dict(start=0,end=1,speaker='')])
def test_invalid_model_timing_cannot_reach_subtitle_assignments(turn):
    with pytest.raises(ValueError):validate_turns([turn],12)


def test_regular_overlap_is_preserved_but_exclusive_overlap_is_rejected():
    values=[dict(start=0,end=3,speaker='A'),dict(start=1,end=2,speaker='B')]
    assert validate_turns(values,4)==values
    assert overlap_windows(values)==[(1,2)]
    with pytest.raises(ValueError):validate_turns(values,4,exclusive=True)
    # Multiple tracks for the same identity are not multiple voices.
    assert overlap_windows([values[0],dict(start=1,end=2,speaker='A')])==[]


@pytest.fixture
def project(tmp_path,monkeypatch):
    app=create_app(tmp_path/'projects');st=app.state.store
    p=st.create('speaker analysis','en','course');identifier=p['id'];folder=st.folder(identifier);(folder/'cache').mkdir()
    rate=24000;frames=np.arange(12*rate);frequency=np.where(frames<4*rate,220.,660.)
    samples=(3000*np.sin(2*np.pi*frequency*frames/rate)).astype('<i2')
    with wave.open(str(folder/'source.media'),'wb') as output:
        output.setparams((1,2,rate,0,'NONE','not compressed'));output.writeframes(samples.tobytes())
    (folder/'old-reference.wav').write_bytes(b'old voice');(folder/'result.mkv').write_bytes(b'old movie')
    rows=[row(1,.2,3.7),row(2,4.1,7.7),row(3,7.5,10.5),row(4,8,9),row(5,2,5)]
    st.update(identifier,segments=rows,media={'duration':12,'audio':True,'video':False},roles={'speaker_1':{'name':'原角色'}},references={'speaker_1':'old-reference.wav'},outputs={'video':'result.mkv'},dub_signature='old',translation_checks={'3':{'issues':[]}})
    monkeypatch.setattr('app.diarization.readiness',lambda _:dict(ready=True,runtime_ready=True,model_ready=True))
    monkeypatch.setattr('app.diarization_api.readiness',lambda _:dict(ready=True,runtime_ready=True,model_ready=True))
    monkeypatch.setattr('app.diarization.check_environment',lambda _:dict(ok=True,cuda_available=True))
    released=[];monkeypatch.setattr(app.state.pipeline.tts,'release',lambda reason:released.append(reason))
    calls=[]
    def worker(args,log,cancel,**kwargs):
        request=json.loads(Path(args[-1]).read_text(encoding='utf-8'));calls.append((request,kwargs))
        with wave.open(request['audio'],'rb') as audio:assert (audio.getframerate(),audio.getnchannels(),audio.getsampwidth())==(16000,1,2)
        assert kwargs['env']['HF_HUB_OFFLINE']=='1' and kwargs['env']['PYANNOTE_METRICS_ENABLED']=='0'
        assert 'HF_TOKEN' not in kwargs['env'] and 'HUGGING_FACE_HUB_TOKEN' not in kwargs['env']
        regular,exclusive=turns();Path(request['output']).write_text(json.dumps(dict(turns=regular,exclusive=exclusive,device=request['device'])),encoding='utf-8')
    monkeypatch.setattr('app.diarization.run',worker)
    with TestClient(app) as client:yield app,client,st,identifier,folder,calls,released


def settle(app,identifier):
    deadline=time.monotonic()+10
    while app.state.pipeline.busy(identifier) and time.monotonic()<deadline:time.sleep(.01)
    assert not app.state.pipeline.busy(identifier)


def generate(project,**options):
    app,client,st,identifier,folder,calls,released=project
    response=client.post(f'/api/projects/{identifier}/diarization',json=dict(revision=st.read(identifier)['edit_revision'],**options))
    assert response.status_code==200,response.text;settle(app,identifier)
    after=st.read(identifier);assert after['status']=='done',after['error'];return after['diarization_proposal']


def audio_frequency(content):
    import io
    with wave.open(io.BytesIO(content),'rb') as stream:
        rate=stream.getframerate();values=np.frombuffer(stream.readframes(stream.getnframes()),dtype='<i2').astype(float)
    return np.fft.rfftfreq(len(values),1/rate)[np.argmax(abs(np.fft.rfft(values)))]


def test_proposal_keeps_old_outputs_and_samples_distinct_non_overlap_voices(project):
    app,client,st,identifier,folder,calls,released=project;before=st.read(identifier)
    proposal=generate(project,device='cuda',num_speakers=2);after=st.read(identifier)
    assert proposal['status']=='ready' and len(proposal['roles'])==2 and len(proposal['references'])==2
    for key in ['segments','roles','references','outputs','edit_revision','translation_checks']:assert after[key]==before[key]
    assert calls[0][0]['num_speakers']==2 and calls[0][0]['device']=='cuda' and released
    frequencies=[]
    for speaker,name in proposal['references'].items():
        result=client.get(f'/api/projects/{identifier}/files/{name}');assert result.status_code==200
        frequencies.append(round(audio_frequency(result.content)))
        assert proposal['reference_origins'][speaker]['duration']==4
        assert client.get(f'/api/projects/{identifier}/files/{name}',headers={'Range':'bytes=0-99'}).status_code==206
    assert sorted(frequencies)==[220,660]
    assert not list((folder/'cache').glob('diarization-*-16k.wav'))
    assert client.get(f'/api/projects/{identifier}/files/diarization-ref-{"0"*24}-1.wav').status_code==404


def test_cpu_inference_does_not_evict_warm_tts_and_range_is_forwarded(project):
    *_,calls,released=project
    proposal=generate(project,device='cpu',min_speakers=2,max_speakers=4)
    assert not released and proposal['device']=='cpu' and calls[0][0]['min_speakers']==2 and calls[0][0]['max_speakers']==4


def test_adopting_only_selected_roles_backs_up_and_keeps_original_text_and_voices(project):
    app,client,st,identifier,folder,*_=project;before=st.read(identifier);proposal=generate(project,device='cpu')
    choices=[dict(id=item['id'],speaker=item['after']) for item in proposal['items'] if item['after'] and not item['needs_review']]
    result=client.post(f'/api/projects/{identifier}/diarization/apply',json=dict(revision=before['edit_revision'],request_id=proposal['request_id'],assignments=choices))
    assert result.status_code==200,result.text;after=st.read(identifier)
    assert after['outputs']=={} and after['dub_signature']=='' and after['diarization_proposal']['status']=='applied'
    assert after['references']['speaker_1']=='old-reference.wav' and after['roles']['speaker_1']==before['roles']['speaker_1']
    for old,new in zip(before['segments'],after['segments']):
        for key in ['text','zh','start','end','id']:assert old[key]==new[key]
        assert (new['speaker']!='speaker_1')==(old['id'] in [1,2])
    assert len(after['roles'])==3 and after['translation_checks']==before['translation_checks']
    assert list((folder/'history').glob('*/result.mkv')) and list((folder/'history').glob('*/old-reference.wav'))
    assert result.json()['edit_revision']==before['edit_revision']+1
    assert client.post(f'/api/projects/{identifier}/diarization/apply',json=dict(revision=after['edit_revision'],request_id=proposal['request_id'],assignments=choices)).status_code==409


def test_stale_or_invalid_assignments_cannot_overwrite_manual_work(project):
    app,client,st,identifier,folder,*_=project;proposal=generate(project,device='cpu');before=st.read(identifier)
    url=f'/api/projects/{identifier}/diarization/apply';speaker=next(iter(proposal['roles']))
    payload=dict(revision=before['edit_revision'],request_id=proposal['request_id'],assignments=[dict(id=1,speaker=speaker)])
    for changes in [dict(assignments=[dict(id=1,speaker='untrusted')]),dict(assignments=[dict(id=999,speaker=speaker)]),dict(assignments=[dict(id=1,speaker=speaker)]*2)]:
        assert client.post(url,json={**payload,**changes}).status_code==400
    rows=before['segments'];rows[0]['zh']='新手动译文';st.update(identifier,segments=rows)
    assert client.post(url,json=payload).status_code==409
    assert st.read(identifier)['segments'][0]['zh']=='新手动译文'


@pytest.mark.parametrize('cancel',[False,True])
def test_failed_or_cancelled_run_preserves_previous_proposal_and_outputs(project,monkeypatch,cancel):
    app,client,st,identifier,folder,*_=project;proposal=generate(project,device='cpu');before=st.read(identifier)
    def broken(*args,**kwargs):raise Cancelled('cancelled') if cancel else RuntimeError('model failed')
    monkeypatch.setattr('app.diarization.run',broken)
    response=client.post(f'/api/projects/{identifier}/diarization',json=dict(revision=before['edit_revision'],device='cpu'));assert response.status_code==200
    settle(app,identifier);after=st.read(identifier)
    assert after['status']==('cancelled' if cancel else 'failed') and after['diarization_proposal']==proposal
    for key in ['segments','roles','references','outputs']:assert after[key]==before[key]
    assert not list((folder/'cache').glob('diarization-*-16k.wav'))


def test_start_rejects_stale_missing_model_empty_subtitles_and_busy_project(project,monkeypatch):
    app,client,st,identifier,folder,*_=project;before=st.read(identifier);url=f'/api/projects/{identifier}/diarization'
    payload=dict(revision=before['edit_revision'],device='cpu')
    assert client.post(url,json={**payload,'revision':payload['revision']-1}).status_code==409
    assert client.post(url,json={**payload,'num_speakers':0}).status_code==422
    app.state.pipeline.events[identifier]=threading.Event()
    try:assert client.post(url,json=payload).status_code==409
    finally:app.state.pipeline.events.pop(identifier)
    monkeypatch.setattr('app.diarization_api.readiness',lambda _:dict(ready=False))
    assert client.post(url,json=payload).status_code==400
    assert st.read(identifier)['segments']==before['segments']


def test_repeated_analysis_reuses_ids_despite_label_changes_and_keeps_custom_voice(project,monkeypatch):
    app,client,st,identifier,folder,*_=project;first=generate(project,device='cpu')
    chosen=[dict(id=i['id'],speaker=i['after']) for i in first['items'] if i['after'] and not i['needs_review']]
    result=client.post(f'/api/projects/{identifier}/diarization/apply',json=dict(revision=st.read(identifier)['edit_revision'],request_id=first['request_id'],assignments=chosen));assert result.status_code==200
    before=st.read(identifier);speaker=chosen[0]['speaker'];roles={**before['roles'],speaker:{**before['roles'][speaker],'name':'主讲'}}
    (folder/'custom.wav').write_bytes(b'custom voice')
    st.update(identifier,roles=roles,references={**before['references'],speaker:'custom.wav'},voice_assignments={speaker:{'id':'preset','name':'Saved preset'}})
    from app import diarization
    original=diarization.run
    def swapped(args,*a,**kw):
        original(args,*a,**kw);request=json.loads(Path(args[-1]).read_text('utf-8'));output=Path(request['output']);data=json.loads(output.read_text('utf-8'))
        for field in ['turns','exclusive']:
            for turn in data[field]:turn['speaker']='renamed-'+turn['speaker']
        output.write_text(json.dumps(data),encoding='utf-8')
    monkeypatch.setattr(diarization,'run',swapped)
    second=generate(project,device='cpu')
    assert set(second['roles'])==set(first['roles']) and all(role['reused'] for role in second['roles'].values())
    choices=[dict(id=i['id'],speaker=i['after']) for i in second['items'] if i['after'] and not i['needs_review']]
    result=client.post(f'/api/projects/{identifier}/diarization/apply',json=dict(revision=st.read(identifier)['edit_revision'],request_id=second['request_id'],assignments=choices));assert result.status_code==200,result.text
    after=result.json();assert len(after['roles'])==len(before['roles'])
    assert after['roles'][speaker]['name']=='主讲' and after['references'][speaker]=='custom.wav' and after['voice_assignments'][speaker]['id']=='preset'


def test_detected_lines_can_be_mapped_to_existing_manual_role_without_adding_roles(project):
    app,client,st,identifier,folder,*_=project;before=st.read(identifier);proposal=generate(project,device='cpu')
    result=client.post(f'/api/projects/{identifier}/diarization/apply',json=dict(revision=before['edit_revision'],request_id=proposal['request_id'],assignments=[dict(id=1,speaker='speaker_1')]))
    assert result.status_code==200,result.text;after=result.json()
    assert len(after['roles'])==1 and after['references']==before['references']
    assert after['roles']['speaker_1']['name']=='原角色' and after['roles']['speaker_1']['diarization_linked']


def test_ambiguous_speaker_split_does_not_silently_reuse_one_role_twice():
    from app.role_lifecycle import reused_roles,unique_role_name
    p={'roles':{'old':{'name':'说话人 1','origin':'diarization'}},'references':{},'segments':[{**row(1,0,10),'speaker':'old'}]}
    turns=[dict(start=0,end=5,speaker='A'),dict(start=5,end=10,speaker='B')]
    assert reused_roles(p,turns,['A','B'])=={}
    assert unique_role_name(p)=='说话人 2'
