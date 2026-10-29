# SPDX-License-Identifier: LicenseRef-Bilibili-Model-Use
# Adapted from index-tts/index-tts indextts/infer_v2_5.py at
# d9e41aac89fd00b3d71497fddb287b7f24613712. See NOTICE and LICENSES/IndexTTS.txt.
"""Native IndexTTS-2.5 text batching; one model, sequential safe vocoder."""
from __future__ import annotations
import gc
import json
import os
from pathlib import Path
import re
import time
import wave
from collections import deque
from contextlib import contextmanager
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.emotions import emotion_key,emotion_kwargs,emotion_vector


class NotBatchable(ValueError): pass


def groups(clips, size):
    """Schedule by reference and length; output paths retain original segment identity."""
    voices = {}
    for clip in clips:
        voices.setdefault((clip['reference'],emotion_key(clip)), []).append(clip)
    for rows in voices.values():
        short = sorted((r for r in rows if len(r['text']) <= 100), key=lambda r:len(r['text']))
        for start in range(0,len(short),size): yield short[start:start+size]
        for row in rows:
            if len(row['text']) > 100: yield [row]


def report(path, value):
    if not path: return
    target=Path(path); target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.pending.json')
    temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    os.replace(temporary,target)


@contextmanager
def stage_timers(model, times, publish):
    """Time major GPU phases for both native batches and the upstream single path."""
    import torch
    original=[]
    targets=[]
    if hasattr(model,'gpt'): targets.append((model.gpt,'inference_speech','语音生成'))
    if hasattr(model,'semantic_codec'): targets.append((model.semantic_codec,'decode','声学生成'))
    if hasattr(model,'s2mel'): targets.append((model.s2mel.models['cfm'],'inference','声学生成'))
    if hasattr(model,'bigvgan'): targets.append((model.bigvgan,'forward','声码器'))
    def measure(fn,stage):
        def wrapped(*args,**kwargs):
            publish(stage)
            torch.cuda.synchronize(); started=time.perf_counter()
            try: return fn(*args,**kwargs)
            finally:
                torch.cuda.synchronize()
                times[stage]=times.get(stage,0)+time.perf_counter()-started
        return wrapped
    try:
        for obj,name,stage in targets:
            fn=getattr(obj,name);original.append((obj,name,fn));setattr(obj,name,measure(fn,stage))
        yield
    finally:
        for obj,name,fn in original: setattr(obj,name,fn)


def tokens(model, text):
    import torch
    from indextts.infer_v2_5 import apply_pronunciation_annotations
    text = model.text_process.clean_pattern.sub(lambda x: model.text_process.char_rep_map[x.group()], text)
    text = apply_pronunciation_annotations(model.text_process.normalize(text).lower())
    text = re.sub(r"<\|([^|]+)\|>", lambda m: f"<|{m.group(1).upper()}|>", text)
    pieces = model.split_text_by_tokens(text, 120, "<|zh|> ")
    if len(pieces) != 1: raise NotBatchable("Long text uses the original multi-segment path")
    ids = model.tokenizer.encode("<|zh|> " + pieces[0], allowed_special="all")
    return torch.nn.functional.pad(torch.IntTensor(ids), (0, 1), value=1)


def native_batch(model, clips):
    if len({(c['reference'],emotion_key(c)) for c in clips})!=1:
        raise NotBatchable('不同音色、情绪或强度不能共用这一批条件')
    import torch
    from indextts.utils.tokenizer import lang_to_token
    from indextts.utils.common import fade_out_tail, save_pcm_wav
    if model.cache_spk_audio_prompt != clips[0]["reference"] or model.cache_spk_cond is None:
        raise NotBatchable("Reference must be prepared through the original path first")
    encoded=[]
    for clip in clips:
        try: encoded.append(tokens(model,clip['text']))
        except NotBatchable as exc:
            exc.output=clip['output']
            raise
    text_tokens = torch.nn.utils.rnn.pad_sequence(encoded, batch_first=True, padding_value=model.gpt.stop_text_token).to(model.device)
    spk, emo = model.cache_spk_cond, model.cache_emo_cond
    style, prompt, mel = model.cache_s2mel_style, model.cache_s2mel_prompt, model.cache_mel
    with torch.inference_mode():
        lengths = torch.tensor([spk.shape[-1]], device=model.device)
        emo_lengths = torch.tensor([emo.shape[-1]], device=model.device)
        with torch.amp.autocast("cuda", enabled=model.dtype is not None, dtype=model.dtype):
            emotion = model.gpt.merge_emovec(spk, emo, lengths, emo_lengths, alpha=1.0)
            vector=emotion_vector(clips[0])
            if vector:
                from indextts.infer_v2_5 import find_most_similar_cosine
                weights=torch.tensor(vector,device=model.device)
                indices=[find_most_similar_cosine(style,matrix) for matrix in model.spk_matrix]
                vectors=torch.cat([matrix[index].unsqueeze(0) for matrix,index in zip(model.emo_matrix,indices)],0)
                emotion=(weights.unsqueeze(1)*vectors).sum(0).unsqueeze(0)+(1-weights.sum())*emotion
            codes, _ = model.gpt.inference_speech(spk, text_tokens, torch.LongTensor([lang_to_token("ZH")] * len(clips)).to(model.device), emo, cond_lengths=lengths, emo_cond_lengths=emo_lengths, emo_vec=emotion, campplus_embedding=style, wav=clips[0]["reference"], do_sample=True, top_p=.8, top_k=30, temperature=.8, num_return_sequences=1, length_penalty=0.0, num_beams=3, repetition_penalty=10.0, max_generate_length=1500)
        if codes.shape[0] != len(clips): raise RuntimeError("Batch result count mismatch")
        waves = []
        for row in codes:
            stop = (row == model.stop_mel_token).nonzero(as_tuple=False)
            if not len(stop) or int(stop[0]) <= 0: raise RuntimeError("Batch voice did not end normally")
            row = row[:int(stop[0])][None]
            with torch.amp.autocast("cuda", enabled=False):
                semantic = model.semantic_codec.decode(row)
                lengths = torch.LongTensor([int(semantic.shape[1] * 1.72)]).to(model.device)
                condition = model.s2mel.models['length_regulator'](semantic, ylens=lengths, n_quantizers=3, f0=None)[0]
                combined = torch.cat([prompt, condition], dim=1)
                target = model.s2mel.models['cfm'].inference(combined, torch.LongTensor([combined.size(1)]).to(model.device), mel, style, None, 25, inference_cfg_rate=.7)
                target = target[:, :, mel.size(-1):]
                wav = model.bigvgan(target.float()).squeeze().unsqueeze(0).squeeze(1)
            waves.append(fade_out_tail(torch.clamp(32767 * wav, -32767.0, 32767.0).cpu(), 22050))
    # Publish only after every row has been decoded successfully.
    for clip, wav in zip(clips, waves):
        pending = clip["output"] + ".pending.wav"
        save_pcm_wav(pending, wav, 22050)
        os.replace(pending, clip["output"])


def run_clips(model, clips, batch_size=1, stats_path=None, progress_path=None, model_load_seconds=0, model_reused=False):
    import torch
    requested=max(1,min(4,int(batch_size)));cap=requested
    pending=deque(c for group in groups(clips,requested) for c in group)
    started=time.perf_counter();times={};history=[];fallback=0;batches=0;done=0;actual=0;reason='';maximum=0;single_output=None;last_fallback=''
    def publish(phase):
        value=dict(phase=phase,instances=1,model_reused=model_reused,worker_pid=os.getpid(),completed=done,total=len(clips),requested_batch=requested,actual_batch=actual,max_actual_batch=maximum,
            fallback_reason=reason,last_fallback_reason=last_fallback,model_load_seconds=round(model_load_seconds,2),seconds=round(time.perf_counter()-started,2),stage_seconds={k:round(v,3) for k,v in times.items()},native_batches=batches,fallback_batches=fallback,
            peak_allocated_mb=round(torch.cuda.max_memory_allocated()/1024**2))
        report(progress_path,value)
        return value
    with stage_timers(model,times,publish):
        while pending:
            if Path(pending[0]['output']).is_file():
                pending.popleft();done+=1;continue
            first=pending[0]
            ready=model.cache_spk_audio_prompt==first['reference'] and model.cache_spk_cond is not None
            free,_=torch.cuda.mem_get_info();headroom=free+max(0,torch.cuda.memory_reserved()-torch.cuda.memory_allocated())
            allowed=4 if headroom>=4*1024**3 else 2 if headroom>=2*1024**3 else 1
            size=min(cap,allowed);reason=''
            if not ready:
                size=1;reason='先合成一段并准备角色参考声音，随后合批'
            elif len(first['text'])>100 or first['output']==single_output:
                size=1;reason='长文本使用原生分句合成'
            elif allowed<cap: reason=f'显存余量不足，批量上限降为 {allowed}'
            group=[]
            for clip in pending:
                if len(group)>=size or clip['reference']!=first['reference'] or emotion_key(clip)!=emotion_key(first) or (group and (len(clip['text'])>100 or clip['output']==single_output)): break
                group.append(clip)
            actual=len(group)
            if actual==1 and not reason:reason='单段兼容模式' if requested==1 else '本角色待处理片段不足以合批'
            publish('准备参考声音' if not ready else '准备文本')
            try:
                if actual>1:
                    native_batch(model,group);batches+=1
                else:
                    clip=group[0]
                    model.infer(spk_audio_prompt=clip['reference'],text=clip['text'],output_path=clip['output']+'.pending.wav',lang='ZH',duration_factor=1.0,verbose=False,**emotion_kwargs(clip))
                    os.replace(clip['output']+'.pending.wav',clip['output'])
            except (RuntimeError,ValueError) as exc:
                if actual==1: raise
                fallback+=1
                reason=('文本需要分句，改用单段合成' if isinstance(exc,NotBatchable) else f'批量 {actual} 失败，降级重试：{str(exc)[:160]}')
                if isinstance(exc,NotBatchable): single_output=getattr(exc,'output',first['output'])
                else: cap=2 if actual>2 else 1
                last_fallback=reason
                print('TTS batch fallback:',reason,flush=True)
                publish('批处理降级');gc.collect();torch.cuda.empty_cache()
                history.append(dict(batch=actual,ids=[c['id'] for c in group],fallback=reason))
                continue
            maximum=max(maximum,actual)
            history.append(dict(batch=actual,ids=[c['id'] for c in group],reason=reason))
            for _ in group: pending.popleft()
            done+=actual
            print(f'TTS {done}/{len(clips)} batch={actual} reason={reason or "同角色合批"}',flush=True)
            publish('片段已保存')
    torch.cuda.synchronize()
    stats=publish('合成完成')
    stats.update(clips=done,seconds=round(time.perf_counter()-started,4),history=history)
    report(stats_path,stats)
    return stats
