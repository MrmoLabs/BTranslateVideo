from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import ROOT, Settings, atomic_json, index_python, resolve
from .media import Cancelled, check_cancel, extract_audio, ffmpeg, fit_clip, mix_timeline, probe, run, wav_duration
from .alignment import plan_alignment
from .audio_cache import reusable_voice_clip
from .export_media import export_project, preview_existing, audio_mode, original_background
from .store import Store
from .subtitles import render_srt, validate_segments
from .segmentation import semantic_segments, VERSION as SEGMENTATION_VERSION
from .translation import local_translator, translate_batch
from .translation_quality import translation_batches, translation_context, run_workflow
from .project_roles import project_roles,reference_window,UNASSIGNED_SPEAKER
from .translation_proposals import generate_proposal
from .emotions import emotion_fields
from .tts_session import TTSSession
from .progress import ACTIVE, TERMINAL, new_progress, advance, finish, tts_detail
from .model_backends import tts_backend, tts_model_signature, validate_tts, transcribe, synthesize_service
from .languages import target_code, target_files, tts_language

WORKER = ROOT / "scripts" / "model_worker.py"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def make_voice_clip(item,folder,reference,settings,model_signature,reference_hash,target_language="zh"):
    config=emotion_fields(item)
    validate_tts(settings,target_language,[item])
    identity=dict(schema=1,text=item['zh'],speaker=item['speaker'],reference=reference_hash,model=str(resolve(settings.index_model)),model_signature=model_signature,version=settings.index_version)
    if settings.tts_provider!='indextts':
        identity.update(model=settings.tts_name,version=settings.tts_provider,model_signature=tts_model_signature(settings))
        if not tts_backend(settings).reference_audio:identity.update(reference='',speaker=settings.tts_voice)
    if target_code(target_language)!='zh':identity['target_language']=target_language
    if config:identity['expression']=config
    key=fingerprint(identity)
    return dict(id=item['id'],text=item['zh'],reference=str(folder/reference),output=str(folder/'cache'/('tts-'+key+'.wav')),**config,**(dict(lang=target_language.upper()) if target_language!='zh' else {}))


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def dub_signature(project, folder, settings):
    from .human_review import content
    model_dir = resolve(settings.index_model)
    model_signature = tts_model_signature(settings)
    refs = {speaker: file_hash(folder / path) for speaker, path in project["references"].items()} if tts_backend(settings).reference_audio else {}
    identity=dict(schema=5, alignment_options={key:getattr(settings,key) for key in ("alignment_mode","alignment_fallback_speed","alignment_fallback_lead","alignment_fallback_lag")}, alignment_policy='short-original-speed', segments=content(project["segments"]), references=refs, model=str(model_dir), model_signature=model_signature, version=settings.index_version, max_speed=settings.max_speed, max_lead=settings.max_lead, max_lag=settings.max_lag)
    if settings.tts_provider!='indextts':identity.update(model=settings.tts_name,model_signature=tts_model_signature(settings),version=settings.tts_provider)
    if target_code(project)!='zh':identity['target_language']=target_code(project)
    return fingerprint(identity)


def place_start(start, limit, duration, previous_end, settings):
    required = duration / settings.max_speed
    if required <= limit - start:
        return start
    earliest = max(0, start - settings.max_lead, previous_end)
    if earliest >= start:
        return start
    return max(earliest, limit - required - .1)


def offline_env() -> dict:
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    # Use the application's downloaded cache, never another project's environment.
    env["HF_HOME"] = str(ROOT / "models" / "hf-cache")
    if os.name == "nt":
        import importlib.util
        cuda = ROOT / ".venv" / "Lib" / "site-packages" / "nvidia"
        bins = [str(p) for p in cuda.glob("*/bin")]
        env["PATH"] = os.pathsep.join(bins + [env.get("PATH", "")])
    return env


class Pipeline:
    def __init__(self, store: Store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dubbing")
        self.events: dict[str, threading.Event] = {}
        self.futures = {}
        self.lock = threading.RLock()
        self.tts = TTSSession()
        self.model_task = dict(status='idle', error='')
        self.model_log = None

    def tts_status(self):
        with self.lock:
            return {**self.tts.status(), 'control':dict(self.model_task),
                    'application_busy':bool(self.events)}

    def load_tts_model(self, settings):
        """Reserve the shared queue before accepting an explicit, audio-free preload."""
        if settings.tts_provider != 'indextts':
            raise ValueError('当前接口没有标准加载卸载协议，请在本地模型服务中管理驻留')
        from .model_backends import model_readiness
        if not model_readiness(settings)['tts_ready']:
            raise ValueError('配音模型文件或独立环境不完整，请先检查模型路径')
        with self.lock:
            if '__tts_model__' in self.events:
                return self.tts_status()
            if self.events:
                raise ValueError('有任务正在处理或排队，请完成或取消后再手动加载模型')
            cancel=threading.Event()
            folder=ROOT/'runtime/tts-preloads'/str(time.time_ns())
            folder.mkdir(parents=True,exist_ok=False);self.model_log=folder/'process.log'
            self.model_task=dict(status='queued',error='',model=str(resolve(settings.index_model)),version=settings.index_version,processing=new_progress('model'))
            self.events['__tts_model__']=cancel
            try:self.futures['__tts_model__']=self.pool.submit(self._load_tts_model,settings,cancel,folder)
            except Exception as exc:
                self.events.pop('__tts_model__',None);self.futures.pop('__tts_model__',None)
                self.model_task={**self.model_task,'status':'failed','error':str(exc),'processing':finish(self.model_task['processing'],'failed',message='模型加载任务未能加入队列')}
                raise ValueError('模型加载任务未能加入队列，请重新启动应用后重试') from exc
            return self.tts_status()

    def cancel_tts_load(self):
        with self.lock:
            event=self.events.get('__tts_model__')
            if event:
                event.set()
                future=self.futures.get('__tts_model__')
                if future and future.cancel():
                    self.events.pop('__tts_model__',None);self.futures.pop('__tts_model__',None)
                    self.model_task={**self.model_task,'status':'cancelled','processing':finish(self.model_task['processing'],'cancelled',message='排队任务已取消，未开始处理')}
                    return self.tts_status()
                self.model_task={**self.model_task,'status':'cancelling','processing':advance(self.model_task['processing'],status='cancelling',message='正在取消模型加载')}
        return self.tts_status()

    def _load_tts_model(self, settings, cancel, folder):
        try:
            with self.lock:self.model_task={**self.model_task,'status':'loading','processing':advance(self.model_task['processing'],phase='model_load',message='准备配音模型')}
            request=dict(operation='load',clips=[],index_root=str(resolve(settings.index_root)),
                         model=str(resolve(settings.index_model)),version=settings.index_version)
            atomic_json(folder/'request.json',request)
            # In after-task mode an explicit preload stays until the next real task.
            idle=None if settings.tts_keep_loaded or settings.tts_idle_minutes==0 else settings.tts_idle_minutes*60
            def tick():
                with self.lock:self.model_task={**self.model_task,'processing':advance(self.model_task['processing'],phase=self.tts.status()['phase'],message=self.tts.status()['phase'])}
            self.tts.run(folder/'request.json',index_python(settings),WORKER,self.model_log,cancel,offline_env(),idle,on_tick=tick)
            with self.lock:self.model_task={**self.model_task,'status':'ready',**self.tts.run_info,'processing':finish(self.model_task['processing'],'done',message='模型已就绪')}
        except Cancelled:
            with self.lock:self.model_task={**self.model_task,'status':'cancelled','error':'','processing':finish(self.model_task['processing'],'cancelled',message='模型加载已取消')}
        except Exception as exc:
            with self.lock:self.model_task={**self.model_task,'status':'failed','error':str(exc)[-3000:] or type(exc).__name__,'processing':finish(self.model_task['processing'],'failed',message='模型加载失败')}
        finally:
            with self.lock:self.events.pop('__tts_model__',None);self.futures.pop('__tts_model__',None)

    def busy(self, identifier):
        return identifier in self.events

    def submit(self, identifier: str, stage: str, settings: Settings):
        if stage not in {"analyze", "resegment", "retranscribe", "check_asr", "translate", "dub", "export", "preview", "optimize", "audition", "retranslate", "reference", "diarize", "subtitle_review", "all"}:
            raise ValueError("无效处理阶段")
        with self.lock:
            if self.busy(identifier):
                raise ValueError("此项目已在队列中")
            from .project_experience import effective_asr,validate_language,steps
            project=self.store.read(identifier)
            settings=effective_asr(settings,project)
            if stage in {'retranscribe','check_asr'} or stage in {'analyze','all'} and not project['segments']:
                validate_language(settings,project['language'])
            event = threading.Event()
            queued=self.store.update(identifier, status="queued", stage=stage, error="", progress=0, processing=new_progress(stage,steps(project) if stage=='all' else None))
            self.events[identifier] = event
            try:self.futures[identifier]=self.pool.submit(self.execute, identifier, stage, settings, event)
            except Exception as exc:
                self.events.pop(identifier,None);self.futures.pop(identifier,None)
                self.store.update(identifier,status='failed',error='任务未能加入处理队列：'+str(exc),processing=finish(queued['processing'],'failed',message='任务未能加入处理队列'))
                raise ValueError('任务未能加入处理队列，请重新启动应用后重试') from exc

    def cancel(self, identifier):
        with self.lock:
            if identifier in self.events:
                if self.store.read(identifier)['status'] in {'done','failed','cancelled'}:return
                self.events[identifier].set()
                future=self.futures.get(identifier)
                if future and future.cancel():
                    self.events.pop(identifier,None);self.futures.pop(identifier,None)
                    self.finish_progress(identifier,'cancelled','排队任务已取消，未开始处理')
                    self.store.update(identifier,status='cancelled',error='')
                    self.store.log(identifier,'排队任务已取消，未开始处理')
                    return
                project=self.store.read(identifier)
                self.store.update(identifier, status="cancelling", **({"processing":advance(project["processing"],status="cancelling",message="正在取消，已完成片段保留")} if project.get("processing") else {}))
            else:
                project=self.store.read_runtime(identifier)
                progress=project.get('processing')
                if progress and progress['status'] in ACTIVE:
                    # A persisted progress snapshot cannot keep a task active
                    # after its queue entry has gone away. Preserve a known
                    # terminal outcome; otherwise cancel only the stale state.
                    status=project['status'] if project['status'] in TERMINAL else 'cancelled'
                    self.store.update_runtime(identifier,status=status,processing=finish(progress,status,message='任务已结束，已清理残留进度'))

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.tts.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def progress(self, identifier, stage, percent, message, detail=None):
        detail=dict(detail or {})
        with self.store.lock:
            project=self.store.read_runtime(identifier)
            old=project.get('processing') or new_progress(stage)
            detail.setdefault('phase',stage);detail.setdefault('message',message)
            value=advance(old,**detail)
            if value!=old or project.get('progress')!=percent or project.get('stage')!=stage:
                logs=project.get('logs',[])
                if not logs or logs[-1]['message']!=message:
                    from .store import now
                    logs=(logs+[dict(time=now(),message=message)])[-200:]
                self.store.update_runtime(identifier,stage=stage,progress=percent,processing=value,logs=logs)

    def begin_stage(self,identifier,stage):
        with self.store.lock:
            project=self.store.read(identifier)
            self.store.update(identifier,processing=advance(project.get('processing') or new_progress(stage),step=stage,phase=stage,message='准备处理'))

    def finish_progress(self,identifier,status,message):
        with self.store.lock:
            project=self.store.read(identifier)
            if project.get('processing'):self.store.update(identifier,processing=finish(project['processing'],status,message=message))

    def execute(self, identifier, stage, settings, cancel):
        folder = self.store.folder(identifier)
        try:
            (folder / "cache").mkdir(exist_ok=True)
            check_cancel(cancel)
            translation_incomplete=False
            from .project_experience import goal,effective_asr
            project=self.store.read(identifier);settings=effective_asr(settings,project)
            project_goal=goal(project)
            self.store.update(identifier, status="running")
            self.begin_stage(identifier,"analyze" if stage in {"resegment","retranscribe","all"} else stage)
            if stage in {"dub","audition"} or stage=='all' and project_goal=='dub':validate_tts(settings,self.store.read(identifier))
            if stage == "resegment":
                self.analyze(identifier, settings, cancel, force=True)
            if stage == 'retranscribe':
                self.analyze(identifier,settings,cancel,force=True,rerecognize=True)
            if stage == 'check_asr':
                self.check_asr(identifier,settings,cancel)
            if stage in {"analyze", "all"}:
                self.begin_stage(identifier,"analyze")
                self.analyze(identifier, settings, cancel)
            if stage=='translate' or stage=='all' and project_goal!='transcribe':
                self.begin_stage(identifier,"translate")
                self.translate(identifier, settings, cancel)
                translation_incomplete=any(not row["zh"].strip() for row in self.store.read(identifier)["segments"])
            if (stage=='dub' or stage=='all' and project_goal=='dub') and not translation_incomplete:
                self.begin_stage(identifier,"dub")
                self.dub(identifier, settings, cancel)
            if (stage=='export' or stage=='all' and project_goal=='dub') and not translation_incomplete:
                self.begin_stage(identifier,"export")
                self.export(identifier, settings, cancel)
            if stage == "optimize":
                self.tts.release("为译文优化释放显存")
                generate_proposal(self.store, identifier, settings, cancel, self.progress)
            if stage == "preview":
                self.preview(identifier, settings, cancel)
            if stage == 'audition':
                self.audition(identifier,settings,cancel)
            if stage == 'retranslate':
                self.retranslate(identifier,settings,cancel)
            if stage == 'reference':
                self.extract_reference(identifier,settings,cancel)
            if stage == 'subtitle_review':
                from .subtitle_review import run_review
                run_review(self,identifier,settings,cancel)
            if stage == 'diarize':
                from .diarization import run_diarization
                run_diarization(self,identifier,settings,cancel)
            # Serialize terminal transitions with cancel(): it must never write
            # "cancelling" over an already completed cancellation or failure.
            with self.lock:
                check_cancel(cancel)
                message='短音频试识别通过，原字幕未修改' if stage=='check_asr' else "处理完成"
                if translation_incomplete:
                    remaining=sum(not row["zh"].strip() for row in self.store.read(identifier)["segments"])
                    message=f"自动翻译已结束，{remaining} 句待人工处理；请在字幕校对中筛选‘译文待核对’，补齐后再生成配音。"
                self.finish_progress(identifier,"done",message)
                if stage=='all' and translation_incomplete:
                    processing=self.store.read(identifier)['processing']
                    processing['completed_steps']=[step for step in processing['completed_steps'] if step in {'analyze','translate'}]
                    self.store.update(identifier,processing=processing)
                self.store.update(identifier, status="done", progress=100)
                self.store.log(identifier, "本阶段完成")
        except Cancelled as exc:
            with self.lock:
                self.finish_progress(identifier,"cancelled","任务已取消，已完成片段保留")
                self.store.update(identifier, status="cancelled", error=str(exc))
                self.store.log(identifier, str(exc))
        except Exception as exc:
            with self.lock:
                self.finish_progress(identifier,"failed","处理失败，请查看错误与日志")
                self.store.update(identifier, status="failed", error=str(exc))
                self.store.log(identifier, "处理失败：" + str(exc)[-2500:])
        finally:
            with self.lock:
                self.events.pop(identifier, None)
                self.futures.pop(identifier,None)

    def audio(self, identifier, settings, cancel):
        folder = self.store.folder(identifier)
        target = folder / "cache" / "source-16k.wav"
        if not target.exists():
            temporary = target.with_suffix(".pending.wav")
            duration=self.store.read(identifier).get('media',{}).get('duration')
            extract_audio(folder / "source.media", temporary, settings, folder / "process.log", cancel, sample_rate=16000,
                progress_duration=duration,on_progress=lambda done,total:self.progress(identifier,'analyze',5,'提取原片音频',dict(phase='audio_extract',done=done,total=total,unit='seconds')))
            os.replace(temporary, target)
        return target

    def snapshot(self, identifier):
        folder = self.store.folder(identifier)
        destination = folder / "history" / str(time.time_ns())
        destination.mkdir(parents=True, exist_ok=False)
        project = self.store.read(identifier)
        atomic_json(destination / "project.json", project)
        for name in {"original.srt", "bilingual.srt", "result.mkv", *target_files(project).values(), *project.get("outputs",{}).values(), *((project.get("audition") or {}).get(k,"") for k in ["file","video","captions"]), *project["references"].values(), *((project.get("diarization_proposal") or {}).get("references",{}).values())}:
            source = folder / name
            if source.is_file() and source.parent==folder:
                if source.suffix in {".srt",".vtt"}:shutil.copy2(source,destination/name)
                else:
                    try:os.link(source,destination/name)
                    except OSError:shutil.copy2(source,destination/name)
        return destination.name

    def check_asr(self,identifier,settings,cancel):
        from .project_experience import asr_profile
        from .store import now
        project=self.store.read(identifier);folder=self.store.folder(identifier)
        cache=folder/'cache/asr-check';cache.mkdir(exist_ok=True)
        duration=min(10,project['media']['duration'])
        self.progress(identifier,'check_asr',5,'准备前 10 秒试识别音频')
        audio=cache/'sample.wav';output=cache/'result.json'
        extract_audio(folder/'source.media',audio,settings,folder/'process.log',cancel,duration=duration,sample_rate=16000)
        self.tts.release('为试识别释放显存')
        transcribe(settings,audio,output,project['language'],project['mode'],folder/'process.log',cancel,offline_env(),
                   on_progress=lambda value:self.progress(identifier,'check_asr',10,value.get('message','试识别'),value))
        rows=json.loads(output.read_text(encoding='utf-8'))
        if not rows:raise ValueError('试识别范围内没有语音，请调整素材或直接识别完整原片')
        from .segmentation import flatten_words
        flatten_words(rows,duration)
        self.store.update(identifier,asr_check=dict(signature=asr_profile(settings,project['language'])['signature'],
            completed_at=now(),text=''.join(row['text'] for row in rows)[:1000],duration=duration))

    def analyze(self, identifier, settings, cancel, force=False, rerecognize=False):
        project = self.store.read(identifier)
        if project["segments"] and not force:
            self.store.log(identifier, "已有字幕，复用当前校对结果；可用‘重新断句’恢复完整句意")
            return
        stage = 'retranscribe' if rerecognize else "resegment" if force else "analyze"
        self.progress(identifier, stage, 5, "取得词级时间戳，随后按完整句意断句")
        folder = self.store.folder(identifier)
        audio = self.audio(identifier, settings, cancel)
        model = resolve(settings.asr_model)
        model_file = model / "model.bin"
        metadata = dict(schema=2, source=file_hash(folder / "source.media"), model=str(model), model_mtime=model_file.stat().st_mtime_ns if model_file.exists() else 0, language=project["language"], mode=project["mode"])
        if settings.asr_provider=='fun_asr':
            from .fun_asr import cache_signature
            metadata.update(cache_signature(settings),model=str(resolve(settings.fun_asr_model)),model_mtime=0)
        elif settings.asr_provider!='faster_whisper':metadata.update(provider=settings.asr_provider,url=settings.asr_url,model=settings.asr_name,model_mtime=0,revision=settings.asr_revision)
        raw_path = folder / "cache" / "asr.json"
        meta_path = folder / "cache" / "asr-words-metadata.json"
        cached = False
        if raw_path.is_file() and meta_path.is_file():
            cached = json.loads(meta_path.read_text(encoding="utf-8")) == metadata
        if force and not rerecognize:
            if not raw_path.is_file() or not meta_path.is_file():raise ValueError('没有可复用的识别缓存，请选择重新识别原声')
            previous_metadata=json.loads(meta_path.read_text(encoding='utf-8'))
            if previous_metadata.get('source')!=metadata['source']:raise ValueError('原片与识别缓存不一致，请选择重新识别原声')
            cached=True
        if rerecognize:cached=False
        if not cached:
            self.tts.release("为语音识别释放显存")
            transcribe(settings,audio,raw_path,project["language"],project["mode"],folder/"process.log",cancel,offline_env(),
                on_progress=lambda value:self.progress(identifier,stage,10,value.get('message','识别原文'),value))
            atomic_json(meta_path, metadata)
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.progress(identifier, stage, 40, "本地模型恢复完整句子，校验全部原词与时间轴",dict(phase="segmentation_model"))
        self.tts.release("为本地翻译释放显存")
        with local_translator(settings, folder / "process.log", cancel) as url:
            segments = semantic_segments(raw, project["media"]["duration"], url, settings, project["mode"], cancel, folder / "cache", lambda done, total: self.progress(identifier, stage, 40 + round(done / total * 55), f"语义断句 {done}/{total} 词",dict(phase="segmentation",done=done,total=total,unit="words")))
        segments = validate_segments(segments, project["media"]["duration"])
        check_cancel(cancel)
        if project["segments"]:
            snapshot = self.snapshot(identifier)
            self.store.log(identifier, "重断句前的字幕、译文与成片已备份到 history/" + snapshot)
        from .project_experience import asr_profile,last_asr
        from .store import now
        provenance=last_asr(self.store,identifier,project) if cached else {**asr_profile(settings,project['language']), 'completed_at':now()}
        self.store.update(identifier, segments=segments, outputs={}, dub_signature="", segmentation_version=SEGMENTATION_VERSION, dub_timing=[],asr_last=provenance)
        self.write_subtitles(identifier)
        self.progress(identifier, stage, 100, f"识别及语义断句完成，共 {len(segments)} 个完整句意单元")

    def translate(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        segments = validate_segments(project["segments"], project["media"]["duration"])
        checks=dict(project.get("translation_checks",{}))
        pending = [i for i, item in enumerate(segments) if not item["zh"]]
        if not pending:
            self.store.log(identifier, "已有全部译文，保留校对结果")
            return
        self.progress(identifier, "translate", 5, "载入本地翻译模型；已完成译文将被保留")
        self.tts.release("为本地翻译释放显存")
        with local_translator(settings, self.store.folder(identifier) / "process.log", cancel) as url:
            completed = skipped = translated = 0
            self.progress(identifier,"translate",5,"开始翻译与语义审校",dict(phase="translation_batches",done=0,total=len(pending),unit="segments"))
            for indices in translation_batches(segments, pending, settings.translation_batch):
                batch = [segments[i] for i in indices]
                batch_checks={};failures={}
                def failed(row,error):
                    # Keep invalid output out of the editable translation and audio.
                    failures[row['id']]=str(error)[:1000]
                result = translate_batch(url, settings, batch, translation_context(segments, indices), project["mode"], cancel,
                    cache=self.store.folder(identifier) / "cache", topic=dict(title=project["name"], language=project["language"], target_language=target_code(project), mode=project["mode"]),on_checks=batch_checks.update,on_failure=failed)
                check_cancel(cancel)
                for index in indices:
                    row=segments[index]
                    if row['id'] in failures:
                        checks[str(row['id'])]=dict(source=row['text'],translation=row['zh'],target_language=target_code(project),
                            issues=[dict(code='translation_failed',message='本句自动翻译失败，已跳过；请听原声后人工校对或重新翻译。',detail=failures[row['id']])])
                        self.store.log(identifier,f"第 {row['id']} 段翻译已跳过，等待人工处理：{failures[row['id']]}")
                        continue
                    row["zh"]=result[row["id"]]
                    checks.pop(str(row['id']),None)
                    if row['id'] in batch_checks:checks[str(row['id'])]=dict(source=row['text'],translation=row['zh'],target_language=target_code(project),issues=batch_checks[row['id']])
                self.store.update(identifier, segments=segments, outputs={},translation_checks=checks)
                if batch_checks:self.store.log(identifier,"译文已保存，需人工核对第 "+", ".join(map(str,sorted(batch_checks)))+" 段；可在字幕页筛选‘待核对’")
                completed += len(indices);skipped+=len(failures);translated+=len(result)
                self.progress(identifier, "translate", round(100 * completed / len(pending)), f"已处理 {completed}/{len(pending)} 段：译文通过 {translated} 段，跳过待处理 {skipped} 段",dict(phase="translation_batches",done=completed,total=len(pending),unit="segments",reused=len(segments)-len(pending)))
        if skipped:
            self.progress(identifier,'translate',100,f"自动翻译已结束，{skipped} 句已跳过，请在字幕校对中处理。",dict(phase='translation_review_required',done=completed,total=len(pending),unit='segments',reused=len(segments)-len(pending)))
        self.write_subtitles(identifier)

    def write_subtitles(self, identifier):
        folder = self.store.folder(identifier)
        project=self.store.read(identifier);files=target_files(project)
        segments = project["segments"]
        for name, language, bilingual in [("original.srt", "text", False), (files["subtitles"], "zh", False), ("bilingual.srt", "zh", True)]:
            (folder / name).write_text(render_srt(segments, language, bilingual), encoding="utf-8", newline="\n")

        from .subtitles import render_vtt
        (folder / files["captions"]).write_text(render_vtt(segments), encoding="utf-8", newline="\n")

    def retranslate(self,identifier,settings,cancel):
        project=self.store.read(identifier);request=project.get('retranslation_request')
        if not request:raise ValueError('请先选择需要重新翻译的句子')
        if request['revision']!=project.get('edit_revision',0):raise ValueError('字幕已经更新，请重新发起单句翻译')
        index=next(i for i,row in enumerate(project['segments']) if row['id']==request['segment_id'])
        row=project['segments'][index];folder=self.store.folder(identifier)
        mode=request.get('mode','translate')
        if mode!='translate':
            from .cue_refinement import generate_candidate
            candidate=generate_candidate(self,identifier,project,row,index,request,settings,cancel)
            check_cancel(cancel);current=self.store.read(identifier)
            if current.get('edit_revision',0)!=request['revision'] or target_code(current)!=target_code(project):raise ValueError('处理期间字幕或目标语言已经更新，建议尚未采用')
            self.store.update(identifier,retranslation=dict(request_id=request['id'],segment_id=row['id'],source=row['text'],before=row['zh'],
                revision=request['revision'],mode=mode,target_language=target_code(project),status='ready',**candidate))
            self.progress(identifier,'retranslate',100,'本句校对建议已生成，核对采用后才修改字幕',dict(phase='cue_review',done=1,total=1,unit='segments'))
            return
        self.progress(identifier,'retranslate',0,f"重新翻译第 {row['id']} 句；原译文保持不变",dict(phase='translation_batches',done=0,total=1,unit='segments'))
        self.tts.release('为单句翻译释放显存')
        with local_translator(settings,folder/'process.log',cancel) as url:
            result=run_workflow(url,settings,[row],translation_context(project['segments'],[index]),project['mode'],cancel,existing=True,
                topic=dict(title=project['name'],language=project['language'],target_language=target_code(project),mode=project['mode']),cache=None)
        check_cancel(cancel)
        current=self.store.read(identifier)
        if current.get('edit_revision',0)!=request['revision'] or target_code(current)!=target_code(project):raise ValueError('翻译期间字幕或目标语言已经更新，本次结果未采用')
        item=result[row['id']]
        self.store.update(identifier,retranslation=dict(request_id=request['id'],segment_id=row['id'],source=row['text'],before=row['zh'],after=item['zh'],revision=request['revision'],
            reason=item.get('reason',''),checks=item.get('checks',[]),target_language=target_code(project),status='ready'))
        self.progress(identifier,'retranslate',100,'单句新译文已生成，对比采用后才替换原文',dict(phase='translation_batches',done=1,total=1,unit='segments'))

    def extract_reference(self,identifier,settings,cancel):
        project=self.store.read(identifier);request=project.get('reference_request')
        if not request:raise ValueError('请先选择需要从原片取样的角色')
        if project.get('edit_revision',0)!=request['revision']:raise ValueError('字幕已经更新，请重新选择角色取样')
        speaker=request['speaker'];window=reference_window(project,speaker);folder=self.store.folder(identifier)
        name=f'reference-{speaker}-{uuid.uuid4().hex}.wav';target=folder/name
        self.progress(identifier,'reference',0,'从已分配台词中提取原片声音；不会自动识别多人身份',dict(phase='reference_extract',done=0,total=window['duration'],unit='seconds'))
        try:
            extract_audio(folder/'source.media',target,settings,folder/'process.log',cancel,start=window['start'],duration=window['duration'],
                progress_duration=window['duration'],on_progress=lambda done,total:self.progress(identifier,'reference',round(95*done/total),'正在提取角色参考音',dict(phase='reference_extract',done=done,total=total,unit='seconds')))
            check_cancel(cancel)
            if wav_duration(target)<2.95:raise ValueError('提取的声音不足 3 秒，请上传参考音')
            with self.lock:
                current=self.store.read(identifier)
                if current.get('edit_revision',0)!=request['revision']:raise ValueError('取样期间字幕已更新，本次参考音未采用')
                self.snapshot(identifier)
                assignments=dict(current.get('voice_assignments',{}));assignments.pop(speaker,None)
                origins={**current.get('reference_origins',{}),speaker:dict(kind='project',**window)}
                self.store.update(identifier,references={**current['references'],speaker:name},voice_assignments=assignments,reference_origins=origins,
                    outputs={},dub_signature='',export_signature='',audition=None,audition_request=None,
                    reference_result=dict(request_id=request['id'],speaker=speaker,**window))
            self.progress(identifier,'reference',100,'参考音已从原片提取，请在角色卡片试听确认',dict(phase='reference_extract',done=window['duration'],total=window['duration'],unit='seconds'))
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def references(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        folder = self.store.folder(identifier)
        if not tts_backend(settings).reference_audio:
            return {item["speaker"]:"" for item in project["segments"]}
        refs = dict(project["references"])
        speakers = sorted({item["speaker"] for item in project["segments"] if item["speaker"]!=UNASSIGNED_SPEAKER})
        for speaker in speakers:
            if speaker in refs and (folder / refs[speaker]).is_file():
                continue
            # Automatic extraction is safe only for the default single-speaker project.
            if len(speakers) != 1:
                raise ValueError(f"请为角色 {speaker} 上传干净参考声音，再进行多人配音")
            choices = [item for item in project["segments"] if item["speaker"] == speaker and item["end"] - item["start"] >= 3]
            if not choices:
                raise ValueError("找不到至少 3 秒的参考片段，请手动上传参考声音")
            selected = max(choices, key=lambda item: min(10, item["end"] - item["start"]))
            path = f"reference-{speaker}.wav"
            duration = min(10, selected["end"] - selected["start"])
            if audio_mode(project) == "original":
                bed, _ = original_background(self.store, identifier, settings, cancel, self.progress, offline_env(), before_gpu=lambda: self.tts.release("为背景分离释放显存"))
                ffmpeg(settings, ["-ss", str(selected["start"]), "-t", str(duration), "-i", str(folder / "source.media"), "-ss", str(selected["start"]), "-t", str(duration), "-i", str(bed), "-filter_complex", "[1:a]aeval='-val(0)|-val(1)'[inverse];[0:a][inverse]amix=inputs=2:duration=shortest:normalize=0[voice]", "-map", "[voice]", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(folder / path)], folder / "process.log", cancel)
                self.store.log(identifier, "已从分离对白提取干净参考声音，减少背景音乐对音色克隆的干扰")
            else:
                extract_audio(self.audio(identifier, settings, cancel), folder / path, settings, folder / "process.log", cancel, start=selected["start"], duration=duration)
            refs[speaker] = path
            self.store.log(identifier, "已自动提取参考声音，建议先试听；若含音乐或其他人声，可上传替换")
        self.store.update(identifier, references=refs)
        return refs

    def synthesize(self,identifier,pending,total,settings,cancel,stage='dub'):
        folder=self.store.folder(identifier)
        if pending and settings.tts_provider!='indextts':
            self.tts.release('切换为用户管理的本地配音服务')
            def report(done,count):
                self.store.update_runtime(identifier,tts_runtime=dict(phase='本地服务合成',instances=0,completed=done,total=count,reused=total-count,requested_batch=1,actual_batch=1,provider=settings.tts_provider,model=settings.tts_name))
                self.progress(identifier,stage,5+round(done/count*45),f'本地配音服务已生成 {done}/{count} 段',dict(phase='tts_service',done=done,total=count,unit='segments',reused=total-count))
            synthesize_service(settings,pending,total,folder,cancel,report)
            return
        if pending:
            self.progress(identifier, stage, 5, f"准备或复用 IndexTTS；生成 {len(pending)} 段，复用 {total - len(pending)} 段")
            # A fresh snapshot per request avoids stale reports and Windows
            # readers locking a previous job's progress file during startup.
            tts_progress=folder/'cache'/('tts-progress-'+uuid.uuid4().hex+'.json')
            request = dict(index_root=str(resolve(settings.index_root)), model=str(resolve(settings.index_model)), version=settings.index_version, clips=pending, batch_size=settings.tts_batch_size,progress=str(tts_progress),stats=str(folder/'cache'/'tts-stats.json'))
            self.store.update_runtime(identifier,tts_runtime=dict(phase='等待 GPU',instances=1,completed=0,total=len(pending),reused=total-len(pending),requested_batch=settings.tts_batch_size,actual_batch=0))
            atomic_json(folder / "cache" / "tts-request.json", request)
            completed = -1
            model_reported = False
            last_runtime = None
            last_poll = None
            use_session = bool(settings.tts_idle_minutes or settings.tts_keep_loaded or self.tts.status()["loaded"])
            def synthesis_progress(force=False):
                nonlocal completed, model_reported, last_runtime, last_poll
                now=time.monotonic()
                if not force and last_poll is not None and now-last_poll<.75:return
                last_poll=now
                count = sum(Path(clip["output"]).is_file() for clip in pending)
                runtime = dict(instances=1, completed=count, total=len(pending),
                               requested_batch=settings.tts_batch_size, actual_batch=0)
                if use_session:
                    runtime.update(self.tts.run_info, phase=self.tts.status()['phase'])
                    if not model_reported:
                        message = ('复用已加载模型，无需重新加载' if runtime.get('model_reused')
                                   else '需要加载配音模型：' + runtime.get('model_load_reason', '尚未加载'))
                        self.store.log(identifier, message)
                        model_reported = True
                if tts_progress.is_file():
                    try:
                        runtime.update(json.loads(tts_progress.read_text(encoding='utf-8')))
                    except (OSError,ValueError): pass
                # Saved audio is authoritative when a progress snapshot was skipped.
                runtime['completed']=count
                runtime['total']=len(pending)
                runtime['reused']=total-len(pending)
                if runtime!=last_runtime:
                    self.store.update_runtime(identifier,tts_runtime=runtime)
                    self.progress(identifier,stage,5+round(count/len(pending)*45),runtime.get('phase','准备配音'),tts_detail(runtime))
                    last_runtime=runtime
                if count != completed:
                    completed = count
                    self.progress(identifier, stage, 5 + round(count / len(pending) * 45), f"IndexTTS 已生成 {count}/{len(pending)} 段",tts_detail(runtime))
            if use_session:
                self.tts.run(folder/'cache'/'tts-request.json', index_python(settings), WORKER,
                             folder/'process.log', cancel, offline_env(), None if settings.tts_keep_loaded else settings.tts_idle_minutes*60,
                             on_tick=synthesis_progress)
            else:
                self.tts.release('已选择每次释放模型')
                run([index_python(settings), WORKER, "tts", folder / "cache" / "tts-request.json"], folder / "process.log", cancel, cwd=resolve(settings.index_root), env=offline_env(), on_tick=synthesis_progress)
            synthesis_progress(force=True)
        else:
            self.store.update_runtime(identifier,tts_runtime=dict(phase='全部复用缓存',instances=0,completed=0,total=0,reused=total,requested_batch=settings.tts_batch_size,actual_batch=0))

    def audition(self,identifier,settings,cancel):
        project=self.store.read(identifier);validate_tts(settings,project);request=project.get('audition_request')
        if not request: raise ValueError('请先选择一句生成试听')
        row=validate_segments([request['segment']],project['media']['duration'])[0]
        validate_tts(settings,project,[row])
        refs=dict(project['references'])
        if tts_backend(settings).reference_audio and row['speaker'] not in refs: refs=self.references(identifier,settings,cancel)
        if not tts_backend(settings).reference_audio:refs={row['speaker']:''}
        if row['speaker'] not in refs: raise ValueError('请先为这个角色上传参考声音')
        folder=self.store.folder(identifier);model=resolve(settings.index_model)
        signature=tts_model_signature(settings)
        clip=make_voice_clip(row,folder,refs[row['speaker']],settings,signature,file_hash(folder/refs[row['speaker']]) if refs[row['speaker']] else '',target_code(project))
        raw=Path(clip['output']);reusable=reusable_voice_clip(raw,lambda path,_:self.store.log(identifier,f'已隔离损坏的配音缓存 {path.name}，将重新生成。'))
        self.synthesize(identifier,[] if reusable else [clip],1,settings,cancel,'audition')
        check_cancel(cancel)
        if wav_duration(raw)<=0: raise ValueError('试听音频无有效内容')
        from .generated_speech import remember_clips_safely
        remember_clips_safely(self.store,identifier,[row],[raw],target_code(project))
        video={}
        if request.get('video'):
            from .audition_video import make_audition_video
            video=make_audition_video(project,folder,row,raw,settings,cancel,lambda value,message,detail=None:self.progress(identifier,'audition',value,message,detail))
        check_cancel(cancel)
        name='audition-'+raw.stem.removeprefix('tts-')+'.wav'
        temporary=folder/(name+'.pending.wav');shutil.copyfile(raw,temporary);os.replace(temporary,folder/name)
        from .store import now
        self.store.update(identifier,audition=dict(request_id=request['id'],file=name,segment=row,created=now(),**video))
        self.progress(identifier,'audition',100,'本句视频预览已生成；字幕和成片未被替换' if video else '本句原速试听已生成；字幕和成片未被替换')

    def dub(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        validate_tts(settings,project,project["segments"])
        segments = validate_segments(project["segments"], project["media"]["duration"])
        if any(not item["zh"] for item in segments):
            raise ValueError("请先完成全部译文")
        if settings.tts_provider=="indextts" and not index_python(settings).is_file():
            raise ValueError("IndexTTS 独立环境未安装，请运行 setup.ps1")
        refs = self.references(identifier, settings, cancel)
        folder = self.store.folder(identifier)
        clips, pending, pending_outputs = [], [], set()
        model_dir = resolve(settings.index_model)
        if settings.tts_provider=="indextts" and not model_dir.is_dir():
            raise ValueError("IndexTTS 模型目录不存在")
        model_signature = tts_model_signature(settings)
        hashes = {speaker: file_hash(folder / reference) if reference else "" for speaker, reference in refs.items()}
        for item in segments:
            clip=make_voice_clip(item,folder,refs[item['speaker']],settings,model_signature,hashes[item['speaker']],target_code(project))
            path=Path(clip['output'])
            clips.append((item, path))
            if clip["output"] not in pending_outputs and not reusable_voice_clip(path,lambda path,_:self.store.log(identifier,f'已隔离损坏的配音缓存 {path.name}，将重新生成。')):
                pending.append(clip)
                pending_outputs.add(clip["output"])
        if pending and len(pending)<len(clips):
            from .audio_alignment import preflight_cached,AlignmentBlocked
            self.progress(identifier,'dub',3,'先检查已缓存语音的时间，避免重复长时间合成',dict(phase='audio_preflight'))
            preflight=preflight_cached(segments,[path for _,path in clips],project['media']['duration'],settings,folder/'cache',cancel)
            if preflight:
                preflight['revision']=project.get('edit_revision',0)
                atomic_json(folder/'cache'/'alignment-report.json',preflight)
                self.store.update(identifier,alignment_report=preflight)
                raise AlignmentBlocked(preflight)
        self.synthesize(identifier,pending,len(clips),settings,cancel)
        from .generated_speech import remember_clips_safely
        remember_clips_safely(self.store,identifier,[row for row,_ in clips],[path for _,path in clips],target_code(project))
        self.progress(identifier,'dub',55,'复用已生成语音，规划完整时间轴',dict(phase='alignment_plan'))
        from .audio_alignment import prepare_alignment,AlignmentBlocked
        def preparing(done,total,message):
            self.progress(identifier,'dub',55,message,dict(phase='audio_prepare',done=done,total=total,unit='segments',reused=len(clips)))
        try:
            plan,paths,durations,alignment_settings,edges,alignment=prepare_alignment(segments,[path for _,path in clips],project['media']['duration'],settings,folder/'cache',cancel,preparing)
        except AlignmentBlocked as exc:
            report={**exc.report,'revision':project.get('edit_revision',0)}
            atomic_json(folder/'cache'/'alignment-report.json',report)
            self.store.update(identifier,alignment_report=report)
            raise
        clips=list(zip(segments,paths))
        alignment['revision']=project.get('edit_revision',0)
        self.store.update(identifier,alignment_report={**alignment,'status':'aligning'})
        if alignment['strategy']!='normal':
            self.store.log(identifier,f"自适应对齐：保留原语音与译文；收紧 {alignment['trimmed_count']} 段两端静音共 {alignment['trimmed_seconds']:.2f} 秒；本轮上限 {alignment_settings.max_speed:.2f} 倍速、提前 {alignment_settings.max_lead:g} 秒、延后 {alignment_settings.max_lag:g} 秒。")
        early = sum(slot["start"] < row["start"] - .001 for row, slot in zip(segments, plan))
        late = sum(slot["start"] > row["start"] + .001 for row, slot in zip(segments, plan))
        self.store.log(identifier, f"完整时间轴规划完成：{early} 段利用前方空白，{late} 段适度延后（最多 {settings.max_lag:g} 秒），保持全文与倍速上限")
        self.progress(identifier, "dub", 55, f"CPU 并行对齐：{settings.audio_workers} 个任务；已有缓存会复用",dict(phase="audio_align",done=0,total=len(clips),unit="segments"))

        def fit_one(index):
            row, source = clips[index]
            slot, duration = plan[index], durations[index]
            stat = source.stat()
            key = fingerprint(dict(schema=1, id=row["id"], source=source.name, size=stat.st_size, mtime=stat.st_mtime_ns, window=round(slot["window"], 8), max_speed=alignment_settings.max_speed))
            destination = folder / "cache" / f"fit-{row['id']}-{key}.wav"
            check_cancel(cancel)
            reusable = False
            if destination.is_file():
                try:
                    fitted=wav_duration(destination)
                    reusable = fitted <= min(slot["window"],duration) + 1 / 24000
                    if duration <= slot['window']:
                        reusable = reusable and abs(fitted-duration) <= 1 / 24000 + .000001
                except Exception:
                    pass
            if not reusable:
                temporary = destination.with_suffix(".pending.wav")
                try:
                    speed=fit_clip(source, temporary, slot["window"], alignment_settings, folder / "cache" / f"fit-{row['id']}.log", cancel)
                    os.replace(temporary, destination)
                except ValueError as exc:
                    raise ValueError(f"第 {row['id']} 段：{exc}") from exc
            fitted = wav_duration(destination)
            if reusable or speed is None:speed=max(1,duration/fitted)
            if fitted>duration+1/24000+.000001 or (duration<=slot['window'] and abs(fitted-duration)>1/24000+.000001):
                raise ValueError(f"第 {row['id']} 段配音时长被改变；短配音必须保持原速，未替换音轨")
            return index, (slot["start"], destination), dict(id=row["id"], start=slot["start"], end=slot["start"] + fitted,speed=round(speed,5),trim_lead=round(edges[index]["lead_frames"]/edges[index]["rate"],5),trim_tail=round(edges[index]["tail_frames"]/edges[index]["rate"],5))

        placed, timing = [None] * len(clips), [None] * len(clips)
        executor = ThreadPoolExecutor(max_workers=settings.audio_workers, thread_name_prefix="audio-fit")
        futures = [executor.submit(fit_one, index) for index in range(len(clips))]
        last_progress = 0
        try:
            for completed, future in enumerate(as_completed(futures), 1):
                index, clip, record = future.result()
                placed[index], timing[index] = clip, record
                if time.monotonic() - last_progress >= .7 or completed == len(clips):
                    self.progress(identifier, "dub", 55 + round(completed / len(clips) * 35), f"CPU 并行配音对齐 {completed}/{len(clips)}",dict(phase="audio_align",done=completed,total=len(clips),unit="segments"))
                    last_progress = time.monotonic()
        except BaseException:
            cancel.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if not any(a["end"] > b["start"] + .00001 for a, b in zip(segments, segments[1:])):
            if any(a["end"] > b["start"] + 1 / 24000 + .00001 for a, b in zip(timing, timing[1:])):
                raise ValueError("实际配音对齐出现新增重叠，未替换音轨，请重新调整时间")
        audio_name=target_files(project)["audio"]
        target = folder / (Path(audio_name).stem+".pending.wav")
        self.progress(identifier,"dub",95,"合并已对齐的人声音轨",dict(phase="audio_merge"))
        mix_timeline(placed, project["media"]["duration"], target, cancel)
        os.replace(target, folder / audio_name)
        signature = dub_signature(self.store.read(identifier), folder, settings)
        alignment.update(status='ready',max_speed_used=round(max(item['speed'] for item in timing),3),over_normal_count=sum(item['speed']>settings.max_speed+.00001 for item in timing))
        atomic_json(folder/'cache'/'alignment-report.json',{**alignment,'cues':timing})
        self.store.update(identifier, dub_signature=signature, dub_timing=timing, outputs={"audio": audio_name},alignment_report=alignment)
        self.write_subtitles(identifier)
        self.progress(identifier, "dub", 100, "译制音轨生成完成，可试听后导出视频")

    def export(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        folder = self.store.folder(identifier)
        if not project.get("dub_signature") or not (folder / target_files(project)["audio"]).exists():
            raise ValueError("请先生成当前版本的配音")
        if project["dub_signature"] != dub_signature(project, folder, settings):
            raise ValueError("配音设置、模型或参考声音已变化，请重新生成配音后导出")
        self.write_subtitles(identifier)
        export_project(self.store, identifier, settings, cancel, self.progress, offline_env(), before_gpu=lambda: self.tts.release("为背景分离释放显存"))

    def preview(self, identifier, settings, cancel):
        self.write_subtitles(identifier)
        preview_existing(self.store, identifier, settings, cancel, self.progress)
