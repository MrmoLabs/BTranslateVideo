"""Reviewable translation proposals; original subtitles/media stay untouched until apply."""
from __future__ import annotations
import json
import re
from .config import atomic_json, resolve
from .media import check_cancel
from .store import now
from .subtitles import validate_segments
from .languages import target_code
from .translation import local_translator
from .translation_quality import VERSION, digest, run_workflow, translation_batches, translation_context, risk_reasons, speech_seconds

def read_proposal(store, identifier):
    project=store.read(identifier); meta=project.get("translation_proposal")
    if not meta: return None
    if not re.fullmatch(r"[0-9a-f]{24}",meta["id"]): raise ValueError("无效优化建议")
    path=store.folder(identifier)/"proposals"/(meta["id"]+".json")
    data=json.loads(path.read_text(encoding="utf-8"))
    data["stale"]=meta.get("status")!="ready" or data["base_revision"]!=project.get("edit_revision",0) or data["base_signature"]!=digest(project["segments"])
    return data

def generate_proposal(store,identifier,settings,cancel,progress):
    project=store.read(identifier); segments=validate_segments(project["segments"],project["media"]["duration"])
    indices=[i for i,r in enumerate(segments) if r["zh"]]
    if not indices: raise ValueError("请先生成或导入译文，再优化已有译文")
    folder=store.folder(identifier); topic=dict(title=project["name"],language=project["language"],target_language=target_code(project),mode=project["mode"])
    model=resolve(settings.translation_model)
    model_id=(str(model),model.stat().st_size,model.stat().st_mtime_ns) if model.is_file() else str(model)
    signature=digest([VERSION,segments,project.get("edit_revision",0),topic,model_id,settings.translation_model_id,settings.glossary,settings.translation_compact,settings.translation_service_url] + ([settings.translation_revision] if settings.translation_revision else []))
    journal=folder/"cache"/("proposal-"+signature+".json")
    records={}
    if journal.is_file():
        try: records={int(k):v for k,v in json.loads(journal.read_text(encoding="utf-8")).items()}
        except (ValueError,TypeError): records={}
    progress(identifier,"optimize",2,"生成语序与语义优化建议；原译文、配音和成片保持可用")
    pending=[i for i in indices if segments[i]["id"] not in records]
    if pending:
        with local_translator(settings,folder/"process.log",cancel) as url:
            for group in translation_batches(segments,pending,settings.translation_batch):
                batch=[segments[i] for i in group]
                failures={}
                def failed(row,error):failures[row["id"]]=str(error)[:1000]
                result=run_workflow(url,settings,batch,translation_context(segments,group,settings),project["mode"],cancel,existing=True,topic=topic,cache=folder/"cache",on_failure=failed)
                check_cancel(cancel)
                for row in batch:
                    item=result.get(row["id"])
                    if item is None:
                        issue=dict(code='translation_failed',message='本句自动翻译失败，已跳过；请听原声后人工校对或重新翻译。',detail=failures[row['id']])
                        item=dict(zh=row['zh'],reason='本句自动审校失败，已跳过并保留旧译文。',checks=[issue],estimated_seconds=speech_seconds(row['zh'],target_code(project)))
                        checks=dict(store.read(identifier).get('translation_checks',{}))
                        checks[str(row['id'])]=dict(source=row['text'],translation=row['zh'],target_language=target_code(project),issues=[issue])
                        store.update(identifier,translation_checks=checks)
                        store.log(identifier,f"第 {row['id']} 段审校已跳过，原译文保持不变：{failures[row['id']]}")
                    records[row["id"]]=dict(id=row["id"],source=row["text"],before=row["zh"],after=item["zh"],reason=item["reason"],checks=item.get("checks",[]),risk=risk_reasons(row)+[check["message"] for check in item.get("checks",[])],estimated_seconds=item["estimated_seconds"],seconds=round(row["end"]-row["start"],2))
                atomic_json(journal,{key:value for key,value in records.items() if not any(check['code']=='translation_failed' for check in value.get('checks',[]))})
                progress(identifier,"optimize",round(95*len(records)/len(indices)),f"已审校 {len(records)}/{len(indices)} 段；尚未替换译文",dict(phase="translation_review",done=len(records),total=len(indices),unit="segments"))
    check_cancel(cancel)
    items=[records[segments[i]["id"]] for i in indices]
    changed=sum(r["before"]!=r["after"] for r in items)
    failed_count=sum(any(check["code"]=="translation_failed" for check in item.get("checks",[])) for item in items)
    data=dict(id=signature,version=VERSION,base_revision=project.get("edit_revision",0),base_signature=digest(segments),created=now(),items=items,total=len(items),changed=changed,failed=failed_count)
    atomic_json(folder/"proposals"/(signature+".json"),data)
    store.update(identifier,translation_proposal=dict(id=signature,status="ready",total=len(items),changed=changed,failed=failed_count,created=data["created"]))
    progress(identifier,"optimize",100,f"优化建议已就绪：{changed}/{len(items)} 段有调整，{failed_count} 段跳过待人工处理；查看对比后可应用所选建议",dict(phase="translation_review_required" if failed_count else "translation_review",done=len(items),total=len(items),unit="segments"))
    return data

def selected_changes(store,identifier,proposal_id,revision,ids):
    project=store.read(identifier); data=read_proposal(store,identifier)
    if not data or data["id"]!=proposal_id or data["stale"] or revision!=project.get("edit_revision",0):
        raise ValueError("字幕或优化建议已经更新，请重新生成建议，当前校对未被覆盖")
    if not ids or len(ids)!=len(set(ids)): raise ValueError("请选择不重复的优化段落")
    choices={r["id"]:r for r in data["items"] if r["before"]!=r["after"]}
    if any(i not in choices for i in ids): raise ValueError("所选段落不属于本次可应用的优化建议")
    segments=[dict(r) for r in project["segments"]]
    for row in segments:
        if row["id"] in ids:
            item=choices[row["id"]]
            if row["zh"]!=item["before"] or row["text"]!=item["source"]: raise ValueError("译文已变化，请重新生成优化建议")
            row["zh"]=item["after"]
    return validate_segments(segments,project["media"]["duration"])
