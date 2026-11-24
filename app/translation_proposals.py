"""Reviewable translation proposals; original subtitles/media stay untouched until apply."""
from __future__ import annotations
import json
import re
from uuid import uuid4
from .config import atomic_json, resolve
from .media import check_cancel
from .store import now
from .subtitles import validate_segments
from .languages import target_code
from .model_backends import translation_parallelism
from .translation import local_translator, log_network_batch
from .translation_quality import VERSION, digest, run_workflow, translation_results, risk_reasons, speech_seconds
from .paragraph_translation import uses_paragraph_translation

def read_journal(path):
    try:
        return {int(k):v for k,v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        return {}


def source_position(row):
    return [row["start"],row["end"],row.get("speaker","speaker_1")]


def proposal_source_positions(path, data, segments, backup):
    if "source_positions" in data:
        return data["source_positions"]
    if digest(segments)!=data["base_signature"]:
        try:
            segments=json.loads(backup.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            return {}
        if digest(segments)!=data["base_signature"]:
            return {}
    positions={str(row["id"]):source_position(row) for row in segments}
    # Keep the verified positions with legacy proposals before a later edit
    # replaces the rolling segments.previous.json checkpoint.
    atomic_json(path,{**data,"source_positions":positions})
    return positions


def read_proposal(store, identifier, *, include_partial=False):
    project=store.read(identifier)
    preview=project.get("translation_preview")
    revision=project.get("edit_revision",0)
    if include_partial and preview and preview["base_revision"]==revision:
        cache_id=preview["cache_id"]
        if not re.fullmatch(r"[0-9a-f]{24}",cache_id):
            raise ValueError("无效翻译候选")
        records=read_journal(store.folder(identifier)/"cache"/f"proposal-{cache_id}.json")
        items=[records[row["id"]] for row in project["segments"] if row["id"] in records] if preview["done"] else []
        return dict(id=preview["id"],base_revision=revision,total=preview["total"],items=items,partial=True)
    meta=project.get("translation_proposal")
    if not meta: return None
    if not re.fullmatch(r"[0-9a-f]{24}",meta["id"]): raise ValueError("无效优化建议")
    path=store.folder(identifier)/"proposals"/(meta["id"]+".json")
    data=json.loads(path.read_text(encoding="utf-8"))
    segments=project["segments"]
    current_signature=digest(segments)
    ready=meta.get("status")=="ready"
    retranslation=meta.get("kind")=="retranslate_all"
    data["stale"]=not ready or data["base_revision"]!=revision or data["base_signature"]!=current_signature
    if ready and retranslation:
        positions=proposal_source_positions(path,data,segments,store.folder(identifier)/"segments.previous.json")
        if data["stale"] and positions:
            by_id={row["id"]:row for row in segments}
            retained=[]
            for item in data["items"]:
                row=by_id.get(item["id"])
                if (row and positions.get(str(item["id"]))==source_position(row)
                        and row["text"]==item["source"] and row["zh"]==item["before"]):
                    retained.append(item)
            if retained:
                data.update(items=retained,total=len(retained),changed=sum(item["after"]!=item["before"] for item in retained),
                            failed=sum(any(issue["code"]=="translation_failed" for issue in item.get("checks",[])) for item in retained),
                            needs_retranslation=len(segments)-len(retained),base_revision=revision,base_signature=current_signature,stale=False)
    data.pop("source_positions",None)
    data["allow_edits"]=retranslation and not data["stale"]
    return data

def generate_proposal(store,identifier,settings,cancel,progress,*,retranslate=False):
    project=store.read(identifier); segments=validate_segments(project["segments"],project["media"]["duration"])
    source_rows=[{**row,"zh":""} for row in segments] if retranslate else segments
    indices=list(range(len(segments))) if retranslate else [i for i,r in enumerate(segments) if r["zh"]]
    if not indices: raise ValueError("请先生成或导入译文，再优化已有译文")
    stage="retranslate_all" if retranslate else "optimize"
    phase="translation_batches" if retranslate else "translation_review"
    action="重新翻译" if retranslate else "审校"
    folder=store.folder(identifier); topic=dict(title=project["name"],language=project["language"],target_language=target_code(project),mode=project["mode"])
    model=resolve(settings.translation_model)
    model_id=(str(model),model.stat().st_size,model.stat().st_mtime_ns) if model.is_file() else str(model)
    signature=digest([VERSION,segments,project.get("edit_revision",0),topic,model_id,settings.translation_model_id,settings.glossary,settings.translation_compact,settings.translation_service_url]
                     + ([settings.translation_revision] if settings.translation_revision else [])
                     + (['paragraph'] if uses_paragraph_translation(settings) else [])
                     + ([stage,settings.translation_provider,settings.translation_review,settings.translation_json_mode] if retranslate else [])
                     + (['system_prompt',settings.translation_system_prompt] if settings.translation_system_prompt else []))
    journal=folder/"cache"/("proposal-"+signature+".json")
    previous=project.get("translation_proposal") or {}
    finished=previous.get("cache_id")==signature and previous.get("status")=="ready" and not previous.get("failed")
    records={} if retranslate and finished else read_journal(journal)
    proposal_id=uuid4().hex[:24] if retranslate else signature
    if retranslate:
        preview=dict(id=proposal_id,kind=stage,cache_id=signature,base_revision=project.get("edit_revision",0),
                     total=len(indices),done=len(records))
        store.update_runtime(identifier, translation_preview=preview)
    progress(identifier,stage,2,"从原文生成新译文，完成后可对比采用" if retranslate else "生成语序与语义优化建议；原译文、配音和成片保持可用")
    pending=[i for i in indices if segments[i]["id"] not in records]
    if pending:
        parallel=translation_parallelism(settings) if retranslate else 1
        options=dict(parallel=parallel) if parallel>1 else {}
        with local_translator(settings,folder/"process.log",cancel,**options) as url:
            def request(group,context):
                batch=[source_rows[i] for i in group]
                failures={}
                def failed(row,error):failures[row["id"]]=str(error)[:1000]
                with log_network_batch(settings, [row['id'] for row in batch], lambda message: store.log(identifier, message)):
                    result=run_workflow(url,settings,batch,context,project["mode"],cancel,
                        existing=not retranslate,topic=topic,cache=None if retranslate else folder/"cache",on_failure=failed)
                return result,failures

            for group,(result,failures) in translation_results(source_rows,pending,settings,parallel,request):
                check_cancel(cancel)
                for index in group:
                    row=segments[index]
                    item=result.get(row["id"])
                    if item is None:
                        issue=dict(code='translation_failed',message='本句自动翻译失败，已跳过；请听原声后人工校对或重新翻译。',detail=failures[row['id']])
                        item=dict(zh=row['zh'],reason=f'本句{action}失败，已跳过。',checks=[issue],estimated_seconds=speech_seconds(row['zh'],target_code(project)))
                        if not retranslate:
                            checks=dict(store.read(identifier).get('translation_checks',{}))
                            checks[str(row['id'])]=dict(source=row['text'],translation=row['zh'],target_language=target_code(project),issues=[issue])
                            store.update(identifier,translation_checks=checks)
                        store.log(identifier,f"第 {row['id']} 段{action}已跳过，原译文保持不变：{failures[row['id']]}")
                    records[row["id"]]=dict(id=row["id"],source=row["text"],before=row["zh"],after=item["zh"],reason=item["reason"],checks=item.get("checks",[]),risk=risk_reasons(row)+[check["message"] for check in item.get("checks",[])],estimated_seconds=item["estimated_seconds"],seconds=round(row["end"]-row["start"],2))
                atomic_json(journal,{key:value for key,value in records.items() if not any(check['code']=='translation_failed' for check in value.get('checks',[]))})
                if retranslate:
                    preview["done"]=len(records)
                    store.update_runtime(identifier, translation_preview=preview)
                progress(identifier,stage,round(95*len(records)/len(indices)),f"已{action} {len(records)}/{len(indices)} 段；尚未替换译文",dict(phase=phase,done=len(records),total=len(indices),unit="segments"))
    check_cancel(cancel)
    items=[records[segments[i]["id"]] for i in indices]
    changed=sum(r["before"]!=r["after"] for r in items)
    failed_count=sum(any(check["code"]=="translation_failed" for check in item.get("checks",[])) for item in items)
    data=dict(id=proposal_id,version=VERSION,base_revision=project.get("edit_revision",0),base_signature=digest(project["segments"]),created=now(),items=items,total=len(items),changed=changed,failed=failed_count)
    if retranslate:
        data["source_positions"]={str(row["id"]):source_position(row) for row in project["segments"]}
    atomic_json(folder/"proposals"/(proposal_id+".json"),data)
    store.update(identifier,translation_preview=None,translation_proposal=dict(id=proposal_id,cache_id=signature,kind=stage,status="ready",total=len(items),changed=changed,failed=failed_count,created=data["created"]))
    progress(identifier,stage,100,f"{action}候选已就绪：{changed}/{len(items)} 段有调整，{failed_count} 段跳过待人工处理；查看对比后可应用所选建议",dict(phase="translation_review_required" if failed_count else phase,done=len(items),total=len(items),unit="segments"))
    return data

def selected_changes(store,identifier,proposal_id,revision,ids,edits=None):
    project=store.read(identifier); data=read_proposal(store,identifier)
    if not data or data["id"]!=proposal_id or data["stale"] or revision!=project.get("edit_revision",0):
        raise ValueError("字幕或优化建议已经更新，请重新生成建议，当前校对未被覆盖")
    selected=set(ids)
    if not ids or len(ids)!=len(selected): raise ValueError("请选择不重复的优化段落")
    choices={r["id"]:r for r in data["items"] if r["before"]!=r["after"]}
    if not selected.issubset(choices): raise ValueError("所选段落不属于本次可应用的优化建议")
    edits=edits or {}
    if edits and (not data["allow_edits"] or not edits.keys()<=selected):
        raise ValueError("修改后的译文必须属于本次选择")
    segments=[dict(r) for r in project["segments"]]
    for row in segments:
        if row["id"] in selected:
            item=choices[row["id"]]
            if row["zh"]!=item["before"] or row["text"]!=item["source"]: raise ValueError("译文已变化，请重新生成优化建议")
            row["zh"]=edits.get(row["id"],item["after"]).strip()
            if not row["zh"]:
                raise ValueError(f"第 {row['id']} 段新译文为空，请填写后再采用")
    return validate_segments(segments,project["media"]["duration"])
