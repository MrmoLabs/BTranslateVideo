"""Run the installed offline translator against held-out English/Japanese cases."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import atomic_json,load_settings
from app.translation import local_translator,translate_batch
from app.translation_quality import VERSION,run_workflow

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--compare-before",type=Path);args=parser.parse_args()
    settings=load_settings(); cancel=threading.Event();out=ROOT/"runtime/translation-upgrade";out.mkdir(parents=True,exist_ok=True)
    cases=json.loads((ROOT/"tests/translation_cases.json").read_text(encoding="utf-8"))
    legacy=None
    if args.compare_before:
        spec=importlib.util.spec_from_file_location("app.translation_baseline",args.compare_before);legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    report=dict(version=VERSION,model=settings.translation_model,results=[],note="Reference-guided manual semantic review is required; model output alone is not proof of correctness.")
    with local_translator(settings,out/"regression-model.log",cancel) as url:
        for i,case in enumerate(cases,1):
            row=dict(id=i,start=3,end=15,text=case["text"],zh="",speaker="speaker_1");context=case.get("context",[])
            started=time.perf_counter();old=legacy.translate_batch(url,settings,[row],context,case["mode"],cancel)[i] if legacy else None
            result=run_workflow(url,settings,[row],context,case["mode"],cancel,topic=dict(title="独立语义回归例句，各例句不是连续叙事",language=case["language"]),cache=out/"regression-cache")[i]
            item=dict(**case,baseline=old,actual=result["zh"],reason=result["reason"],seconds=round(time.perf_counter()-started,3));report["results"].append(item)
            atomic_json(out/"language-regression.json",report);print(json.dumps(item,ensure_ascii=False),flush=True)
    print("Held-out language cases complete. See runtime/translation-upgrade/language-regression.json.",flush=True)

if __name__=="__main__":main()
