"""Resumable batch subtitle candidates, separate from adopted project text."""
from __future__ import annotations
from contextlib import nullcontext
import json
import re
from pathlib import Path
import sys
import uuid
import wave
import httpx
from fastapi import HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from .config import atomic_json,load_settings,resolve,ROOT
from .media import check_cancel,extract_audio,run,Cancelled
from .languages import target_code
from .subtitles import validate_segments
from .translation import local_translator
from .translation_quality import _request,run_workflow,translation_context,TranslationRequestError
from .cue_refinement import corrected_source,selected_words
from .translation_checks import numbers

class ReviewRequest(BaseModel):
    revision:int=Field(ge=0)
    ids:list[int]=Field(min_length=1,max_length=30000)
    mode:Literal['translate','asr','source','translation','both']='translation'

class ReviewApply(BaseModel):
    revision:int=Field(ge=0)
    request_id:str
    ids:list[int]=Field(min_length=1,max_length=30000)
    edits:dict[str,dict[str,str]]=Field(default_factory=dict)

def review_path(store,identifier,request_id):
    if len(request_id)!=24 or any(c not in '0123456789abcdef' for c in request_id):raise ValueError('无效的批量校对编号')
    return store.folder(identifier)/'cache'/('subtitle-review-'+request_id+'.json')

def read_review(store,identifier):
    project=store.read(identifier);meta=project.get('subtitle_review')
    if not meta:return None
    path=review_path(store,identifier,meta['id'])
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

def persist_review(store,identifier,review):
    review['version']=review.get('version',0)+1
    atomic_json(review_path(store,identifier,review['id']),review)
    statuses={key:item['status'] for key,item in review['items'].items()}
    summary={key:review[key] for key in ['id','version','mode','status','job_ids','target_language']}
    summary.update(item_statuses=statuses,done=sum(statuses.get(str(i))!='pending' for i in review['job_ids']),total=len(review['job_ids']),
        ready=sum(s=='ready' for s in statuses.values()),failed=sum(s=='failed' for s in statuses.values()),applied=sum(s=='applied' for s in statuses.values()))
    store.update(identifier,subtitle_review=summary)

def same_row(row,item):
    return row and all(row.get(k)==item.get(k) for k in ['text','zh','start','end','speaker'])

def attach_review(app,store,pipeline,editable):
    @app.get('/api/projects/{identifier}/subtitle-review')
    def get_review(identifier:str,ids:str=''):
        review=read_review(store,identifier)
        if not review:return dict(id=None,items={})
        try:selected={int(i) for i in ids.split(',') if i}
        except ValueError:raise ValueError('无效的句子编号')
        if len(selected)>100:raise ValueError('每次最多读取 100 句候选')
        return dict(id=review['id'],version=review['version'],items={k:v for k,v in review['items'].items() if int(k) in selected})

    @app.post('/api/projects/{identifier}/subtitle-review')
    def start_review(identifier:str,data:ReviewRequest):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier)
            if data.revision!=project.get('edit_revision',0):raise HTTPException(409,'字幕已经更新，请保存后重试')
            ids=list(dict.fromkeys(data.ids));rows={r['id']:r for r in project['segments']}
            if any(i not in rows for i in ids):raise ValueError('所选句子不属于当前项目')
            previous=read_review(store,identifier)
            # Keep candidates for unselected rows; previous rounds remain on disk.
            items=dict(previous['items']) if previous and previous['target_language']==target_code(project) else {}
            request_id=uuid.uuid4().hex[:24]
            for i in ids:items[str(i)]={**rows[i],'status':'pending','mode':data.mode}
            review=dict(id=request_id,version=0,mode=data.mode,status='queued',job_ids=ids,target_language=target_code(project),items=items)
            persist_review(store,identifier,review)
            pipeline.submit(identifier,'subtitle_review',load_settings())
            return dict(request_id=request_id,queued=True)

    @app.post('/api/projects/{identifier}/subtitle-review/resume')
    def resume_review(identifier:str,data:ReviewApply):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier);review=read_review(store,identifier)
            if data.revision!=project.get('edit_revision',0) or not review or review['id']!=data.request_id or review['target_language']!=target_code(project):raise HTTPException(409,'校对任务已更新，请刷新后重试')
            requested=set(data.ids)
            pending=[int(k) for k,item in review['items'].items() if int(k) in requested and item['status'] in {'pending','failed'}]
            if not pending:raise ValueError('所选范围没有需要重试或继续的句子')
            rows={r['id']:r for r in project['segments']}
            for i in pending:
                if not same_row(rows.get(i),review['items'][str(i)]):raise HTTPException(409,'待重试的字幕已经修改，请重新发起批量处理')
                review['items'][str(i)]['status']='pending'
            review.update(job_ids=pending,status='queued');persist_review(store,identifier,review)
            pipeline.submit(identifier,'subtitle_review',load_settings());return dict(queued=True)

    @app.post('/api/projects/{identifier}/subtitle-review/apply')
    def apply_review(identifier:str,data:ReviewApply):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier);review=read_review(store,identifier)
            if data.revision!=project.get('edit_revision',0) or not review or review['id']!=data.request_id or review['target_language']!=target_code(project):raise HTTPException(409,'字幕或校对结果已经更新，候选尚未采用')
            rows=[dict(r) for r in project['segments']];by_id={r['id']:r for r in rows};checks=dict(project.get('translation_checks',{}));adopted=[]
            for i in dict.fromkeys(data.ids):
                item=review['items'].get(str(i));row=by_id.get(i)
                if not item or item['status']!='ready':raise HTTPException(409,'请只采用已生成、尚未采用的候选')
                if not same_row(row,item):raise HTTPException(409,f'第 {i} 句已被修改，候选尚未采用')
                edits=data.edits.get(str(i),{})
                if set(edits)-{'text','zh'}:raise ValueError('候选只能修改原文和译文')
                source=edits.get('text',item['after_source']);translation=edits.get('zh',item['after'])
                if item['mode'] in {'source','asr'} and source!=item['text']:translation=''
                row.update(text=source,zh=translation);row.pop('words',None)
                issues=item.get('checks',[]) if not edits else []
                checks.pop(str(i),None)
                if issues:checks[str(i)]=dict(source=source,translation=translation,target_language=target_code(project),issues=issues)
                adopted.append(i)
            rows=validate_segments(rows,project.get('media',{}).get('duration'))
            pipeline.snapshot(identifier)
            store.update(identifier,segments=rows,translation_checks=checks,outputs={},dub_signature='',export_signature='',audition=None,retranslation=None,status='idle',error='')
            for i in adopted:review['items'][str(i)]['status']='applied'
            persist_review(store,identifier,review);pipeline.write_subtitles(identifier)
            return dict(adopted=adopted,project=store.read(identifier))

def asr_candidates(pipeline,identifier,project,review,settings,cancel,complete):
    """One native worker/model for all selected clips, with durable per-cue outputs."""
    from .pipeline import offline_env
    from .model_backends import transcribe
    folder=pipeline.store.folder(identifier);cache=folder/'cache'/('review-asr-'+review['id']);cache.mkdir(exist_ok=True)
    cached=folder/'cache/source-16k.wav';jobs=[]
    if project.get('media',{}).get('audio') is False:raise ValueError('原视频没有音轨')
    stream=wave.open(str(cached),'rb') if cached.exists() else None
    try:
        for i in review['job_ids']:
            check_cancel(cancel);row=review['items'][str(i)];start=max(0,row['start']-2);end=min(project['media']['duration'],row['end']+2)
            if row['end']-row['start']>300:
                complete(i,error='单句重新识别最多 5 分钟，请调整时间范围');continue
            audio=cache/f'{i}.wav';output=cache/f'{i}.json';output.unlink(missing_ok=True)
            if stream:
                if (stream.getframerate(),stream.getnchannels(),stream.getsampwidth())!=(16000,1,2):raise ValueError('原声缓存格式无效，请重新提取音频')
                stream.setpos(min(stream.getnframes(),round(start*16000)));frames=stream.readframes(round((end-start)*16000))
                with wave.open(str(audio),'wb') as clip:clip.setparams((1,2,16000,0,'NONE','not compressed'));clip.writeframes(frames)
            else:extract_audio(folder/'source.media',audio,settings,folder/'process.log',cancel,start=start,duration=end-start,sample_rate=16000)
            jobs.append(dict(id=i,audio=str(audio),output=str(output),start=start))
            pipeline.progress(identifier,'subtitle_review',0,f'准备第 {i} 句原声',dict(phase='review_audio',done=len(jobs),total=len(review['job_ids']),unit='segments'))
    finally:
        if stream:stream.close()
    consumed=set()
    def collect():
        for job in jobs:
            if job['id'] in consumed or not Path(job['output']).exists():continue
            try:value=json.loads(Path(job['output']).read_text(encoding='utf-8'))
            except (ValueError,OSError):continue
            consumed.add(job['id']);row=review['items'][str(job['id'])]
            if isinstance(value,dict):complete(job['id'],error=value.get('error','识别结果无效'));continue
            text=selected_words(value,row['start']-job['start'],row['end']-job['start'])
            if not text:complete(job['id'],error='本句范围内没有识别到带时间戳的语音，请听原声并调整范围');continue
            complete(job['id'],dict(after_source=text,after='' if text!=row['text'] else row['zh'],checks=[],reason='已从原声重新识别；请核对人名和数字。原文变更后旧译文会清空。'))
    if not jobs:return
    if settings.asr_provider in {'faster_whisper','fun_asr'}:
        request=cache/'request.json'
        if settings.asr_provider=='fun_asr':
            from .fun_asr import worker_request,python_path,language_name
            language_name(project['language'])
            payload=worker_request(settings);python=python_path(settings);worker=ROOT/'scripts/fun_asr_worker.py'
        else:
            payload=dict(model=str(resolve(settings.asr_model)),device=settings.asr_device,compute_type=settings.asr_compute_type)
            python=sys.executable;worker=ROOT/'scripts/asr_review_worker.py'
        atomic_json(request,dict(**payload,language=project['language'],items=jobs))
        try:run([python,worker,request],folder/'process.log',cancel,env=offline_env(),on_tick=collect)
        finally:collect()
    else:
        for job in jobs:
            check_cancel(cancel)
            try:transcribe(settings,Path(job['audio']),Path(job['output']),project['language'],project['mode'],folder/'process.log',cancel,offline_env())
            except (ValueError,KeyError,TypeError) as exc:complete(job['id'],error=str(exc));consumed.add(job['id'])
            collect()
    check_cancel(cancel)
    for job in jobs:
        if job['id'] not in consumed:complete(job['id'],error='识别进程未返回本句结果，请重试')

def run_review(pipeline,identifier,settings,cancel):
    store=pipeline.store;project=store.read(identifier);review=read_review(store,identifier)
    if not review:raise ValueError('请先选择字幕并开始批量校对')
    if review['target_language']!=target_code(project):raise ValueError('目标语言已更改，请重新校对')
    rows={r['id']:r for r in project['segments']};indices={r['id']:i for i,r in enumerate(project['segments'])}
    review['status']='running';persist_review(store,identifier,review)
    def complete(i,candidate=None,error=None):
        item=review['items'][str(i)]
        if error:item.update(status='failed',error=str(error)[:1800])
        else:
            item.update(**candidate,error='',candidate_id=review['id']+':'+str(i))
            item['status']='unchanged' if item['after_source']==item['text'] and item['after']==item['zh'] else 'ready'
        persist_review(store,identifier,review)
        done=sum(review['items'][str(i)]['status']!='pending' for i in review['job_ids'])
        failed=sum(review['items'][str(i)]['status']=='failed' for i in review['job_ids'])
        pipeline.progress(identifier,'subtitle_review',100*done/len(review['job_ids']),f'已处理 {done}/{len(review["job_ids"])} 句，{failed} 句需人工处理',dict(phase='review_candidates',done=done,total=len(review['job_ids']),unit='segments'))
    try:
        pipeline.progress(identifier,'subtitle_review',0,'准备批量校对模型，当前字幕保持不变',dict(phase='review_candidates',done=0,total=len(review['job_ids']),unit='segments'))
        pipeline.tts.release('为批量字幕校对释放显存')
        for i in review['job_ids']:
            if not same_row(rows.get(i),review['items'][str(i)]):raise ValueError('字幕已修改，请重新选择并处理')
        asr_ids=[i for i in review['job_ids'] if review['items'][str(i)]['mode']=='asr']
        asr_set=set(asr_ids)
        text_ids=[i for i in review['job_ids'] if i not in asr_set]
        if not text_ids:asr_candidates(pipeline,identifier,project,{**review,'job_ids':asr_ids},settings,cancel,complete)
        else:
            folder=store.folder(identifier);audio_queue=[]
            audio_available=project.get('media',{}).get('audio') is not False and ((folder/'source.media').is_file() or (folder/'cache/source-16k.wav').is_file())

            def generate(i,url,audio_source=None):
                row=rows[i];mode=review['items'][str(i)]['mode'];context=translation_context(project['segments'],[indices[i]],settings)
                source=audio_source or row['text'];reason='';checks=[]
                if audio_source:
                    if re.search(r'[?？]\s*$',row['text']) and not re.search(r'[?？]',source) and re.search(r'(?:かな|かね|ですか|ますか|でしょうか|だろうか)\s*$',source):source+='？'
                    reason='已自动重新识别本句原声，并据此生成译文；识别人名仍可能有误。'
                    checks.append(dict(code='source_audio_reanalyzed',message='文字校对存在疑点，已自动回听并重新识别本句原声。'))
                elif mode in {'source','both'}:
                    source,reason=corrected_source(url,settings,project,row,context,cancel)
                    if numbers(source)!=numbers(row['text']):checks.append(dict(code='source_number_changed',message='原文候选改变了数字，请听原声核实后再采用。'))
                after='' if source!=row['text'] else row['zh']
                topic=dict(title=project['name'],language=project['language'],target_language=target_code(project),mode=project['mode'])
                # Resolve uncertain source before translating; a text model must
                # not invent quantities just to satisfy the numeric validator.
                ambiguous=any(c['code']=='source_number_changed' for c in checks) or re.search(r'不确定|无法确定|听音|可能存在.*错误|可能为|確認が必要|確認.*必要',reason)
                if mode=='both' and audio_available and not audio_source and ambiguous:return None
                if mode=='translate':result=run_workflow(url,settings,[row],context,project['mode'],cancel,existing=True,topic=topic)[i]
                elif mode in {'translation','both'}:
                    result=_request(url,settings,[{**row,'text':source}],context,project['mode'],cancel,'polish' if row['zh'].strip() and not audio_source else 'draft',{i:row['zh']} if row['zh'].strip() and not audio_source else None,topic)[i]
                else:result=None
                if result:
                    after=result['zh'];reason='；'.join(filter(None,[reason,result.get('reason','')]));checks+=result.get('checks',[])
                    if mode=='both' and audio_available and not audio_source and any(c['code']=='numeric_auto_repaired' for c in checks):return None
                return dict(after_source=source,after=after,reason=reason,checks=checks)

            def defer(i,reason):
                review['items'][str(i)].update(status='pending',error='',audio_repair=dict(status='pending',reason=reason))
                audio_queue.append(i);persist_review(store,identifier,review)
                pipeline.progress(identifier,'subtitle_review',100*sum(review['items'][str(j)]['status']!='pending' for j in review['job_ids'])/len(review['job_ids']),f'第 {i} 句存在疑点，已安排自动回听修复',dict(phase='review_candidates',done=sum(review['items'][str(j)]['status']!='pending' for j in review['job_ids']),total=len(review['job_ids']),unit='segments'))

            with local_translator(settings,folder/'process.log',cancel) as url:
                for i in text_ids:
                    check_cancel(cancel);item=review['items'][str(i)]
                    try:
                        saved=item.get('audio_repair',{})
                        candidate=generate(i,url,saved.get('source') if saved.get('status')=='done' else None)
                        if candidate is None:defer(i,'原文含识别疑点，需结合原声重新校对')
                        else:check_cancel(cancel);complete(i,candidate)
                    except TranslationRequestError as exc:
                        if not exc.recoverable:raise
                        if item['mode']=='both' and audio_available and not item.get('audio_repair',{}).get('source') and exc.validation:defer(i,str(exc))
                        else:complete(i,error=str(exc))
                    except (ValueError,KeyError,TypeError,IndexError) as exc:complete(i,error=str(exc))

            if audio_queue or asr_ids:
                # Release the text model before the shared ASR worker takes the
                # GPU lock; then load text once for all recovered clips.
                pipeline.progress(identifier,'subtitle_review',100*sum(review['items'][str(j)]['status']!='pending' for j in review['job_ids'])/len(review['job_ids']),f'重新识别 {len(audio_queue)+len(asr_ids)} 句原声，完成后继续处理',dict(phase='review_audio',done=0,total=len(audio_queue)+len(asr_ids),unit='segments'))
                recovered={}
                def collect_audio(i,candidate=None,error=None):
                    if i in asr_ids:
                        complete(i,candidate,error);return
                    if error:
                        review['items'][str(i)]['audio_repair'].update(status='failed',error=str(error))
                        complete(i,error='自动回听未成功：'+str(error));return
                    recovered[i]=candidate['after_source']
                    review['items'][str(i)]['audio_repair'].update(status='done',source=candidate['after_source'])
                    persist_review(store,identifier,review)
                try:asr_candidates(pipeline,identifier,project,{**review,'job_ids':asr_ids+audio_queue},settings,cancel,collect_audio)
                except (ValueError,OSError) as exc:
                    check_cancel(cancel)
                    for i in asr_ids+audio_queue:
                        if i not in recovered and review['items'][str(i)]['status']=='pending':collect_audio(i,error=str(exc))
                if recovered:
                    with local_translator(settings,folder/'process.log',cancel) as url:
                        for i,source in recovered.items():
                            check_cancel(cancel)
                            try:complete(i,generate(i,url,source))
                            except TranslationRequestError as exc:
                                if not exc.recoverable:raise
                                complete(i,error='自动回听后译文仍未通过校验：'+str(exc))
                            except (ValueError,KeyError,TypeError,IndexError) as exc:complete(i,error=str(exc))
        check_cancel(cancel);review['status']='done'
    except Cancelled:
        review['status']='cancelled';raise
    except Exception:
        review['status']='failed';raise
    finally:persist_review(store,identifier,review)
