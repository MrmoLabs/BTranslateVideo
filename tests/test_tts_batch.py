import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("btv_tts_batch", Path(__file__).parents[1] / "scripts/tts_batch.py")
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def clips(tmp_path):
    return [dict(id=i, text="short Chinese text", reference="same.wav", output=str(tmp_path / f"{i}.wav")) for i in range(4)]


def test_batch_groups_keep_reference_and_long_text_boundaries(tmp_path):
    rows = clips(tmp_path)
    rows[1]["reference"] = "other.wav"
    rows[2]["text"] = "x" * 101
    assert [len(g) for g in batch.groups(rows, 2)] == [2, 1, 1]
    assert sorted(c['id'] for g in batch.groups(rows,2) for c in g)==[0,1,2,3]
    assert [len(g) for g in batch.groups(clips(tmp_path), 2)] == [2, 2]
    assert [len(g) for g in batch.groups(clips(tmp_path), 1)] == [1, 1, 1, 1]


def test_native_batch_failure_recovers_all_rows_and_disables_further_batches(tmp_path, monkeypatch):
    rows = clips(tmp_path)
    cuda = SimpleNamespace(mem_get_info=lambda: (4 * 1024**3, 16 * 1024**3), memory_reserved=lambda: 0, memory_allocated=lambda: 0, empty_cache=lambda: None, synchronize=lambda: None, max_memory_allocated=lambda: 0)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    inferred = []
    def infer(**kwargs):
        inferred.append(kwargs["text"])
        Path(kwargs["output_path"]).write_bytes(b"complete voice")
    model = SimpleNamespace(cache_spk_audio_prompt="same.wav", cache_spk_cond=True, infer=infer)
    calls = []
    def fail(*_):
        calls.append(True)
        raise RuntimeError("CUDA out of memory")
    monkeypatch.setattr(batch, "native_batch", fail)
    stats = batch.run_clips(model, rows, 2)
    assert len(calls) == 1 and len(inferred) == 4
    assert stats["fallback_batches"] == 1 and stats["native_batches"] == 0
    assert all(Path(c["output"]).read_bytes() == b"complete voice" for c in rows)
    assert not list(tmp_path.glob("*.pending.wav"))


def test_low_gpu_headroom_uses_serial_without_dropping_rows(tmp_path, monkeypatch):
    rows = clips(tmp_path)
    cuda = SimpleNamespace(mem_get_info=lambda: (1024**3, 16 * 1024**3), memory_reserved=lambda: 0, memory_allocated=lambda: 0, synchronize=lambda: None, max_memory_allocated=lambda: 0)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    def infer(**kwargs): Path(kwargs["output_path"]).write_bytes(b"voice")
    model = SimpleNamespace(cache_spk_audio_prompt="same.wav", cache_spk_cond=True, infer=infer)
    monkeypatch.setattr(batch, "native_batch", lambda *_: (_ for _ in ()).throw(AssertionError("Must not batch with low headroom")))
    stats = batch.run_clips(model, rows, 2)
    assert stats["clips"] == 4 and stats["native_batches"] == 0
    assert all(Path(c["output"]).exists() for c in rows)

def fake_cuda(monkeypatch, free=8):
    cuda=SimpleNamespace(mem_get_info=lambda:(free*1024**3,16*1024**3),memory_reserved=lambda:0,memory_allocated=lambda:0,empty_cache=lambda:None,synchronize=lambda:None,max_memory_allocated=lambda:0)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=cuda))

def test_cold_three_clips_warm_only_one_then_batch_two(tmp_path,monkeypatch):
    fake_cuda(monkeypatch);rows=clips(tmp_path)[:3];singles=[];batches=[]
    model=SimpleNamespace(cache_spk_audio_prompt=None,cache_spk_cond=None)
    def infer(**kw):
        singles.append(kw['text']);model.cache_spk_audio_prompt=kw['spk_audio_prompt'];model.cache_spk_cond=True;Path(kw['output_path']).write_bytes(b'voice')
    model.infer=infer
    def native(_model,group):
        batches.append([c['id'] for c in group])
        for c in group:Path(c['output']).write_bytes(b'voice')
    monkeypatch.setattr(batch,'native_batch',native)
    report=tmp_path/'progress.json';stats=batch.run_clips(model,rows,2,progress_path=report)
    assert len(singles)==1 and batches==[[1,2]] and stats['max_actual_batch']==2
    assert __import__('json').loads(report.read_text(encoding='utf-8'))['completed']==3

def test_batch_four_failure_retries_as_two_without_disabling_all_batches(tmp_path,monkeypatch):
    fake_cuda(monkeypatch);rows=clips(tmp_path);sizes=[]
    model=SimpleNamespace(cache_spk_audio_prompt='same.wav',cache_spk_cond=True,infer=lambda **_: (_ for _ in ()).throw(AssertionError('should batch')))
    def native(_model,group):
        sizes.append(len(group))
        if len(group)==4:raise RuntimeError('CUDA out of memory')
        for c in group:Path(c['output']).write_bytes(b'voice')
    monkeypatch.setattr(batch,'native_batch',native)
    stats=batch.run_clips(model,rows,4)
    assert sizes==[4,2,2] and stats['clips']==4 and stats['fallback_batches']==1
    assert stats['max_actual_batch']==2 and 'out of memory' in stats['last_fallback_reason']

def test_unsupported_text_only_falls_back_for_that_clip(tmp_path,monkeypatch):
    fake_cuda(monkeypatch);rows=clips(tmp_path);singles=[];batches=[]
    def infer(**kw):singles.append(kw['text']);Path(kw['output_path']).write_bytes(b'voice')
    model=SimpleNamespace(cache_spk_audio_prompt='same.wav',cache_spk_cond=True,infer=infer)
    def native(_model,group):
        if group[0]['id']==0:raise batch.NotBatchable('long token sequence')
        batches.append([c['id'] for c in group])
        for c in group:Path(c['output']).write_bytes(b'voice')
    monkeypatch.setattr(batch,'native_batch',native)
    stats=batch.run_clips(model,rows,2)
    assert len(singles)==2 and batches==[[1,2]] and stats['clips']==4

def test_unsupported_middle_text_does_not_force_other_rows_to_serial(tmp_path,monkeypatch):
    fake_cuda(monkeypatch);rows=clips(tmp_path);singles=[];batches=[]
    def infer(**kw):singles.append(kw['output_path']);Path(kw['output_path']).write_bytes(b'voice')
    model=SimpleNamespace(cache_spk_audio_prompt='same.wav',cache_spk_cond=True,infer=infer)
    def native(_model,group):
        if any(c['id']==2 for c in group):
            exc=batch.NotBatchable('requires splitting');exc.output=rows[2]['output'];raise exc
        batches.append([c['id'] for c in group])
        for c in group:Path(c['output']).write_bytes(b'voice')
    monkeypatch.setattr(batch,'native_batch',native)
    stats=batch.run_clips(model,rows,4)
    assert batches==[[0,1]] and len(singles)==2 and stats['clips']==4
