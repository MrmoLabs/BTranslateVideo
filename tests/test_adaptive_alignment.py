import array,copy,math,threading,wave
from pathlib import Path
import pytest
from app.config import Settings
from app.audio_alignment import prepare_alignment,quiet_edges,edge_clip,AlignmentBlocked
from app.media import wav_duration


def tone(seconds,rate=24000):return [round(3000*math.sin(2*math.pi*440*i/rate)) for i in range(round(seconds*rate))]
def wav(path,data):
    with wave.open(str(path),'wb') as output:output.setparams((1,2,24000,0,'NONE',''));output.writeframes(array.array('h',data).tobytes())
def row(identifier,start,end):return dict(id=identifier,start=start,end=end,text='Authored source.',zh='原创测试台词。',speaker='speaker_1')
def prepare(rows,paths,duration,settings,folder):return prepare_alignment(rows,paths,duration,settings,folder,threading.Event(),lambda *_:None)


def test_normal_plan_never_touches_audio_or_slows_short_clips(tmp_path,monkeypatch):
    path=tmp_path/'raw.wav';wav(path,tone(.5));before=path.read_bytes()
    monkeypatch.setattr('app.audio_alignment.edge_clip',lambda *_:pytest.fail('normal fit does not need trimming'))
    plan,paths,durations,used,edges,report=prepare([row(1,0,2)],[path],3,Settings(),tmp_path)
    assert paths==[path] and durations==[.5] and report['strategy']=='normal'
    assert path.read_bytes()==before and report['max_speed_used']==1


def test_quiet_edges_keep_padding_and_all_internal_audio(tmp_path,monkeypatch):
    data=[1]*9600+tone(.3)+[0]*16800+tone(.3)+[1]*12000
    path=tmp_path/'raw.wav';wav(path,data);before=path.read_bytes()
    duration,lead,tail=quiet_edges(path)
    assert lead==pytest.approx(.3*24000,abs=120) and tail==pytest.approx(.38*24000,abs=120)
    prepared,meta=edge_clip(path,tmp_path,threading.Event())
    with wave.open(str(prepared),'rb') as stream:actual=array.array('h',stream.readframes(stream.getnframes()))
    assert list(actual)==data[meta['lead_frames']:len(data)-meta['tail_frames']]
    assert path.read_bytes()==before
    stamp=prepared.stat().st_mtime_ns
    monkeypatch.setattr('app.audio_alignment.quiet_edges',lambda *_:pytest.fail('must reuse validated trim cache'))
    assert edge_clip(path,tmp_path,threading.Event())[0]==prepared and prepared.stat().st_mtime_ns==stamp


def test_all_quiet_audio_and_low_volume_audio_are_not_removed(tmp_path):
    path=tmp_path/'silence.wav';wav(path,[1]*(5*24000))
    assert quiet_edges(path)==(5,0,0)
    assert edge_clip(path,tmp_path,threading.Event())[0]==path


def test_adaptive_has_an_explicit_hard_cap_and_leaves_drafts_unchanged(tmp_path):
    path=tmp_path/'raw.wav';wav(path,tone(1.55));rows=[row(1,0,1)];before=copy.deepcopy(rows);settings=Settings()
    plan,paths,durations,used,edges,report=prepare(rows,[path],1.1,settings,tmp_path)
    assert report['strategy']=='adaptive' and used.max_speed==1.5
    assert durations[0]/plan[0]['window']<=used.max_speed
    assert rows==before and settings.max_speed==1.35
    assert report['over_normal_count']==1


def test_strict_mode_reports_the_entire_conflicting_range(tmp_path):
    rows=[row(i+1,i,i+.5) for i in range(5)];paths=[]
    for i in range(5):
        path=tmp_path/f'{i}.wav';wav(path,tone(2));paths.append(path)
    with pytest.raises(AlignmentBlocked) as caught:prepare(rows,paths,5,Settings(alignment_mode='strict'),tmp_path)
    report=caught.value.report
    assert report['status']=='blocked' and len(report['conflicts'])>1
    assert report['conflicts'][-1]['id']==5
    assert not list(tmp_path.glob('edge-*.wav'))


def test_fallback_never_silently_exceeds_cap_or_truncates_speech(tmp_path):
    path=tmp_path/'raw.wav';wav(path,tone(4));before=path.read_bytes()
    with pytest.raises(AlignmentBlocked) as caught:prepare([row(1,0,1)],[path],1,Settings(),tmp_path)
    assert caught.value.report['effective']['speed']==1.5
    assert path.read_bytes()==before and wav_duration(path)==4


def test_cached_speech_retry_finishes_without_tts_and_reports_alignment(tmp_path,monkeypatch):
    from app.store import Store
    from app.pipeline import Pipeline,make_voice_clip,tts_model_signature
    store=Store(tmp_path/'projects');identifier=store.create('local retry','en','course')['id'];folder=store.folder(identifier);(folder/'cache').mkdir()
    settings=Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9999/v1');rows=[row(1,0,1)]
    store.update(identifier,segments=rows,media={'duration':1.1})
    clip=make_voice_clip(rows[0],folder,'',settings,tts_model_signature(settings),'','zh');wav(Path(clip['output']),tone(1.55));calls=[]
    def reuse(self,identifier,pending,total,*args):assert not pending;calls.append(total)
    monkeypatch.setattr(Pipeline,'synthesize',reuse)
    pipeline=Pipeline(store)
    try:
        pipeline.dub(identifier,settings,threading.Event());result=store.read(identifier)
        assert result['alignment_report']['status']=='ready' and result['alignment_report']['effective']['speed']==1.5
        assert wav_duration(folder/result['outputs']['audio'])==pytest.approx(1.1)
        pipeline.dub(identifier,settings,threading.Event());assert calls==[1,1]
        assert store.read(identifier)['segments']==rows
    finally:pipeline.close()


def test_proven_cached_conflict_is_reported_before_more_tts(tmp_path,monkeypatch):
    from app.store import Store
    from app.pipeline import Pipeline,make_voice_clip,tts_model_signature
    store=Store(tmp_path/'projects');identifier=store.create('preflight','en','course')['id'];folder=store.folder(identifier);(folder/'cache').mkdir()
    settings=Settings(tts_provider='openai_speech',tts_url='http://127.0.0.1:9999/v1');rows=[row(1,0,.5),{**row(2,1,2),'zh':'第二句尚未生成。'}]
    store.update(identifier,segments=rows,media={'duration':2})
    clip=make_voice_clip(rows[0],folder,'',settings,tts_model_signature(settings),'','zh');wav(Path(clip['output']),tone(4))
    monkeypatch.setattr(Pipeline,'synthesize',lambda *_:pytest.fail('Known impossible timing must be caught before TTS'))
    pipeline=Pipeline(store)
    try:
        with pytest.raises(AlignmentBlocked):pipeline.dub(identifier,settings,threading.Event())
        report=store.read(identifier)['alignment_report']
        assert report['strategy']=='cached_preflight' and report['pending_count']==1
        assert Path(clip['output']).exists() and not store.read(identifier)['outputs']
    finally:pipeline.close()
