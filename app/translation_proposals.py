"""Reviewable translation proposals; original subtitles/media stay untouched until apply."""
from __future__ import annotations
import json
import re
from .config import atomic_json, resolve
from .media import check_cancel
from .store import now
from .subtitles import validate_segments
from .translation import local_translator
from .translation_quality import VERSION, digest, run_workflow, translation_batches, translation_context, risk_reasons

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
    if not indices: raise ValueError("请先生成或导入中文译文，再优化已有译文")
    folder=store.folder(identifier); topic=dict(title=project["name"],language=project["language"],mode=project["mode"])
    model=resolve(settings.translation_model)
    model_id=(str(model),model.stat().st_size,model.stat().st_mtime_ns) if model.is_file() else str(model)
    signature=digest([VERSION,segments,project.get("edit_revision",0),topic,model_id,settings.translation_name,settings.glossary,settings.translation_compact,settings.translation_url])
    journal=folder/"cache"/("proposal-"+signature+".json")
    records={}
    if journal.is_file():
        try: records={int(k):v for k,v in json.loads(journal.read_text(encoding="utf-8")).items()}
        except (ValueError,TypeError): records={}
    progress(identifier,"optimize",2,"本地生成语序与语义优化建议；原译文、配音和成片保持可用")
    pending=[i for i in indices if segments[i]["id"] not in records]
    if pending:
        with local_translator(settings,folder/"process.log",cancel) as url:
            for group in translation_batches(segments,pending,settings.translation_batch):
                batch=[segments[i] for i in group]
                result=run_workflow(url,settings,batch,translation_context(segments,group),project["mode"],cancel,existing=True,topic=topic,cache=folder/"cache")
                check_cancel(cancel)
                for row in batch:
                    item=result[row["id"]]
                    records[row["id"]]=dict(id=row["id"],source=row["text"],before=row["zh"],after=item["zh"],reason=item["reason"],risk=risk_reasons(row),estimated_seconds=item["estimated_seconds"],seconds=round(row["end"]-row["start"],2))
                atomic_json(journal,records)
                progress(identifier,"optimize",round(95*len(records)/len(indices)),f"已审校 {len(records)}/{len(indices)} 段；尚未替换译文")
    check_cancel(cancel)
    items=[records[segments[i]["id"]] for i in indices]
    changed=sum(r["before"]!=r["after"] for r in items)
    data=dict(id=signature,version=VERSION,base_revision=project.get("edit_revision",0),base_signature=digest(segments),created=now(),items=items,total=len(items),changed=changed)
    atomic_json(folder/"proposals"/(signature+".json"),data)
    store.update(identifier,translation_proposal=dict(id=signature,status="ready",total=len(items),changed=changed,created=data["created"]))
    progress(identifier,"optimize",100,f"优化建议已就绪：{changed}/{len(items)} 段有调整，查看对比后可应用所选建议")
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
