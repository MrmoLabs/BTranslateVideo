from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import ROOT, Settings, atomic_json, index_python, resolve
from .media import Cancelled, check_cancel, extract_audio, ffmpeg, fit_clip, mix_timeline, probe, run, wav_duration
from .alignment import plan_alignment
from .export_media import export_project, preview_existing, audio_mode, original_background
from .store import Store
from .subtitles import render_srt, validate_segments
from .segmentation import semantic_segments, VERSION as SEGMENTATION_VERSION
from .translation import local_translator, translate_batch
from .translation_quality import translation_batches, translation_context
from .translation_proposals import generate_proposal
from .emotions import emotion_fields
from .tts_session import TTSSession

WORKER = ROOT / "scripts" / "model_worker.py"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def make_voice_clip(item,folder,reference,settings,model_signature,reference_hash):
    config=emotion_fields(item)
    identity=dict(schema=1,text=item['zh'],speaker=item['speaker'],reference=reference_hash,model=str(resolve(settings.index_model)),model_signature=model_signature,version=settings.index_version)
    if config:identity['expression']=config
    key=fingerprint(identity)
    return dict(id=item['id'],text=item['zh'],reference=str(folder/reference),output=str(folder/'cache'/('tts-'+key+'.wav')),**config)


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def dub_signature(project, folder, settings):
    model_dir = resolve(settings.index_model)
    model_signature = sorted((str(p.relative_to(model_dir)), p.stat().st_size, p.stat().st_mtime_ns) for p in model_dir.rglob("*") if p.is_file() and ".cache" not in p.parts)
    refs = {speaker: file_hash(folder / path) for speaker, path in project["references"].items()}
    return fingerprint(dict(schema=4, alignment_policy='short-original-speed', segments=project["segments"], references=refs, model=str(model_dir), model_signature=model_signature, version=settings.index_version, max_speed=settings.max_speed, max_lead=settings.max_lead, max_lag=settings.max_lag))


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
        self.lock = threading.RLock()
        self.tts = TTSSession()

    def busy(self, identifier):
        return identifier in self.events

    def submit(self, identifier: str, stage: str, settings: Settings):
        if stage not in {"analyze", "resegment", "translate", "dub", "export", "preview", "optimize", "audition", "all"}:
            raise ValueError("无效处理阶段")
        with self.lock:
            if self.busy(identifier):
                raise ValueError("此项目已在队列中")
            event = threading.Event()
            self.events[identifier] = event
            self.store.update(identifier, status="queued", stage=stage, error="", progress=0)
            self.pool.submit(self.execute, identifier, stage, settings, event)

    def cancel(self, identifier):
        with self.lock:
            if identifier in self.events:
                self.events[identifier].set()
                self.store.update(identifier, status="cancelling")

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.tts.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def progress(self, identifier, stage, percent, message):
        self.store.update(identifier, stage=stage, progress=percent)
        self.store.log(identifier, message)

    def execute(self, identifier, stage, settings, cancel):
        folder = self.store.folder(identifier)
        (folder / "cache").mkdir(exist_ok=True)
        try:
            check_cancel(cancel)
            self.store.update(identifier, status="running")
            if stage == "resegment":
                self.analyze(identifier, settings, cancel, force=True)
            if stage in {"analyze", "all"}:
                self.analyze(identifier, settings, cancel)
            if stage in {"translate", "all"}:
                self.translate(identifier, settings, cancel)
            if stage in {"dub", "all"}:
                self.dub(identifier, settings, cancel)
            if stage in {"export", "all"}:
                self.export(identifier, settings, cancel)
            if stage == "optimize":
                self.tts.release("为译文优化释放显存")
                generate_proposal(self.store, identifier, settings, cancel, self.progress)
            if stage == "preview":
                self.preview(identifier, settings, cancel)
            if stage == 'audition':
                self.audition(identifier,settings,cancel)
            check_cancel(cancel)
            self.store.update(identifier, status="done", progress=100)
            self.store.log(identifier, "本阶段完成")
        except Cancelled as exc:
            self.store.update(identifier, status="cancelled", error=str(exc))
            self.store.log(identifier, str(exc))
        except Exception as exc:
            self.store.update(identifier, status="failed", error=str(exc))
            self.store.log(identifier, "处理失败：" + str(exc)[-2500:])
        finally:
            with self.lock:
                self.events.pop(identifier, None)

    def audio(self, identifier, settings, cancel):
        folder = self.store.folder(identifier)
        target = folder / "cache" / "source-16k.wav"
        if not target.exists():
            temporary = target.with_suffix(".pending.wav")
            extract_audio(folder / "source.media", temporary, settings, folder / "process.log", cancel, sample_rate=16000)
            os.replace(temporary, target)
        return target

    def snapshot(self, identifier):
        folder = self.store.folder(identifier)
        destination = folder / "history" / str(time.time_ns())
        destination.mkdir(parents=True, exist_ok=False)
        project = self.store.read(identifier)
        atomic_json(destination / "project.json", project)
        for name in {"original.srt", "chinese.srt", "bilingual.srt", "chinese.wav", "result.mkv", *project["references"].values()}:
            source = folder / name
            if source.is_file():
                shutil.copy2(source, destination / name)
        return destination.name

    def analyze(self, identifier, settings, cancel, force=False):
        project = self.store.read(identifier)
        if project["segments"] and not force:
            self.store.log(identifier, "已有字幕，复用当前校对结果；可用‘重新断句’恢复完整句意")
            return
        stage = "resegment" if force else "analyze"
        self.progress(identifier, stage, 5, "取得词级时间戳，随后按完整句意断句")
        folder = self.store.folder(identifier)
        audio = self.audio(identifier, settings, cancel)
        model = resolve(settings.asr_model)
        model_file = model / "model.bin"
        metadata = dict(schema=2, source=file_hash(folder / "source.media"), model=str(model), model_mtime=model_file.stat().st_mtime_ns if model_file.exists() else 0, language=project["language"], mode=project["mode"])
        raw_path = folder / "cache" / "asr.json"
        meta_path = folder / "cache" / "asr-words-metadata.json"
        cached = False
        if raw_path.is_file() and meta_path.is_file():
            cached = json.loads(meta_path.read_text(encoding="utf-8")) == metadata
        if not cached:
            request = dict(model=str(model), audio=str(audio), language=project["language"], mode=project["mode"], device=settings.asr_device, compute_type=settings.asr_compute_type, output=str(raw_path))
            atomic_json(folder / "cache" / "asr-request.json", request)
            self.tts.release("为语音识别释放显存")
            run([sys.executable, WORKER, "asr", folder / "cache" / "asr-request.json"], folder / "process.log", cancel, env=offline_env())
            atomic_json(meta_path, metadata)
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.progress(identifier, stage, 40, "本地模型恢复完整句子，校验全部原词与时间轴")
        self.tts.release("为本地翻译释放显存")
        with local_translator(settings, folder / "process.log", cancel) as url:
            segments = semantic_segments(raw, project["media"]["duration"], url, settings, project["mode"], cancel, folder / "cache", lambda done, total: self.progress(identifier, stage, 40 + round(done / total * 55), f"语义断句 {done}/{total} 词"))
        segments = validate_segments(segments, project["media"]["duration"])
        check_cancel(cancel)
        if project["segments"]:
            snapshot = self.snapshot(identifier)
            self.store.log(identifier, "重断句前的字幕、译文与成片已备份到 history/" + snapshot)
        self.store.update(identifier, segments=segments, outputs={}, dub_signature="", segmentation_version=SEGMENTATION_VERSION, dub_timing=[])
        self.write_subtitles(identifier)
        self.progress(identifier, stage, 100, f"识别及语义断句完成，共 {len(segments)} 个完整句意单元")

    def translate(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        segments = validate_segments(project["segments"], project["media"]["duration"])
        pending = [i for i, item in enumerate(segments) if not item["zh"]]
        if not pending:
            self.store.log(identifier, "已有全部中文译文，保留校对结果")
            return
        self.progress(identifier, "translate", 5, "载入本地翻译模型；已完成译文将被保留")
        self.tts.release("为本地翻译释放显存")
        with local_translator(settings, self.store.folder(identifier) / "process.log", cancel) as url:
            completed = 0
            for indices in translation_batches(segments, pending, settings.translation_batch):
                batch = [segments[i] for i in indices]
                result = translate_batch(url, settings, batch, translation_context(segments, indices), project["mode"], cancel,
                    cache=self.store.folder(identifier) / "cache", topic=dict(title=project["name"], language=project["language"], mode=project["mode"]))
                check_cancel(cancel)
                for index in indices:
                    segments[index]["zh"] = result[segments[index]["id"]]
                self.store.update(identifier, segments=segments, outputs={})
                completed += len(indices)
                self.progress(identifier, "translate", round(100 * completed / len(pending)), f"已翻译并检查 {completed}/{len(pending)} 段")
        self.write_subtitles(identifier)

    def write_subtitles(self, identifier):
        folder = self.store.folder(identifier)
        segments = self.store.read(identifier)["segments"]
        for name, language, bilingual in [("original.srt", "text", False), ("chinese.srt", "zh", False), ("bilingual.srt", "zh", True)]:
            (folder / name).write_text(render_srt(segments, language, bilingual), encoding="utf-8", newline="\n")

        from .subtitles import render_vtt
        (folder / "chinese.vtt").write_text(render_vtt(segments), encoding="utf-8", newline="\n")

    def references(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        folder = self.store.folder(identifier)
        refs = dict(project["references"])
        speakers = sorted({item["speaker"] for item in project["segments"]})
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
        if pending:
            self.progress(identifier, stage, 5, f"准备或复用 IndexTTS；生成 {len(pending)} 段，复用 {total - len(pending)} 段")
            tts_progress=folder/'cache'/'tts-progress.json'
            tts_progress.unlink(missing_ok=True)
            request = dict(index_root=str(resolve(settings.index_root)), model=str(resolve(settings.index_model)), version=settings.index_version, clips=pending, batch_size=settings.tts_batch_size,progress=str(tts_progress),stats=str(folder/'cache'/'tts-stats.json'))
            self.store.update(identifier,tts_runtime=dict(phase='等待 GPU',instances=1,completed=0,total=len(pending),reused=total-len(pending),requested_batch=settings.tts_batch_size,actual_batch=0))
            atomic_json(folder / "cache" / "tts-request.json", request)
            completed = -1
            def synthesis_progress():
                nonlocal completed
                count = sum(Path(clip["output"]).is_file() for clip in pending)
                if tts_progress.is_file():
                    try:
                        runtime=json.loads(tts_progress.read_text(encoding='utf-8'))
                        runtime['reused']=total-len(pending)
                        self.store.update(identifier,tts_runtime=runtime)
                    except (OSError,ValueError): pass
                if count != completed:
                    completed = count
                    self.progress(identifier, stage, 5 + round(count / len(pending) * 45), f"IndexTTS 已生成 {count}/{len(pending)} 段")
            if settings.tts_idle_minutes:
                self.tts.run(folder/'cache'/'tts-request.json', index_python(settings), WORKER,
                             folder/'process.log', cancel, offline_env(), settings.tts_idle_minutes*60,
                             on_tick=synthesis_progress)
            else:
                self.tts.release('已选择每次释放模型')
                run([index_python(settings), WORKER, "tts", folder / "cache" / "tts-request.json"], folder / "process.log", cancel, cwd=resolve(settings.index_root), env=offline_env(), on_tick=synthesis_progress)
        else:
            self.store.update(identifier,tts_runtime=dict(phase='全部复用缓存',instances=0,completed=0,total=0,reused=total,requested_batch=settings.tts_batch_size,actual_batch=0))

    def audition(self,identifier,settings,cancel):
        project=self.store.read(identifier);request=project.get('audition_request')
        if not request: raise ValueError('请先选择一句生成试听')
        row=validate_segments([request['segment']],project['media']['duration'])[0]
        refs=dict(project['references'])
        if row['speaker'] not in refs: refs=self.references(identifier,settings,cancel)
        if row['speaker'] not in refs: raise ValueError('请先为这个角色上传参考声音')
        folder=self.store.folder(identifier);model=resolve(settings.index_model)
        signature=sorted((str(p.relative_to(model)),p.stat().st_size,p.stat().st_mtime_ns) for p in model.rglob('*') if p.is_file() and '.cache' not in p.parts)
        clip=make_voice_clip(row,folder,refs[row['speaker']],settings,signature,file_hash(folder/refs[row['speaker']]))
        raw=Path(clip['output']);self.synthesize(identifier,[] if raw.exists() else [clip],1,settings,cancel,'audition')
        check_cancel(cancel)
        if wav_duration(raw)<=0: raise ValueError('试听音频无有效内容')
        name='audition-'+raw.stem.removeprefix('tts-')+'.wav'
        temporary=folder/(name+'.pending.wav');shutil.copyfile(raw,temporary);os.replace(temporary,folder/name)
        from .store import now
        self.store.update(identifier,audition=dict(request_id=request['id'],file=name,segment=row,created=now()))
        self.progress(identifier,'audition',100,'本句原速试听已生成；字幕和成片未被替换')

    def dub(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        segments = validate_segments(project["segments"], project["media"]["duration"])
        if any(not item["zh"] for item in segments):
            raise ValueError("请先完成全部中文翻译")
        if not index_python(settings).is_file():
            raise ValueError("IndexTTS 独立环境未安装，请运行 setup.ps1")
        refs = self.references(identifier, settings, cancel)
        folder = self.store.folder(identifier)
        clips, pending, pending_outputs = [], [], set()
        model_dir = resolve(settings.index_model)
        if not model_dir.is_dir():
            raise ValueError("IndexTTS 模型目录不存在")
        model_signature = sorted((str(p.relative_to(model_dir)), p.stat().st_size, p.stat().st_mtime_ns) for p in model_dir.rglob("*") if p.is_file() and ".cache" not in p.parts)
        hashes = {speaker: file_hash(folder / reference) for speaker, reference in refs.items()}
        for item in segments:
            clip=make_voice_clip(item,folder,refs[item['speaker']],settings,model_signature,hashes[item['speaker']])
            path=Path(clip['output'])
            clips.append((item, path))
            if not path.exists() and clip["output"] not in pending_outputs:
                pending.append(clip)
                pending_outputs.add(clip["output"])
        self.synthesize(identifier,pending,len(clips),settings,cancel)
        self.progress(identifier, "dub", 55, f"时间对齐；共 {len(clips)} 段配音")
        durations = [wav_duration(path) for _row, path in clips]
        plan = plan_alignment(segments, durations, project["media"]["duration"], settings)
        early = sum(slot["start"] < row["start"] - .001 for row, slot in zip(segments, plan))
        late = sum(slot["start"] > row["start"] + .001 for row, slot in zip(segments, plan))
        self.store.log(identifier, f"完整时间轴规划完成：{early} 段利用前方空白，{late} 段适度延后（最多 {settings.max_lag:g} 秒），保持全文与倍速上限")
        self.progress(identifier, "dub", 55, f"CPU 并行对齐：{settings.audio_workers} 个任务；已有缓存会复用")

        def fit_one(index):
            row, source = clips[index]
            slot, duration = plan[index], durations[index]
            stat = source.stat()
            key = fingerprint(dict(schema=1, id=row["id"], source=source.name, size=stat.st_size, mtime=stat.st_mtime_ns, window=round(slot["window"], 8), max_speed=settings.max_speed))
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
                    fit_clip(source, temporary, slot["window"], settings, folder / "cache" / f"fit-{row['id']}.log", cancel)
                    os.replace(temporary, destination)
                except ValueError as exc:
                    raise ValueError(f"第 {row['id']} 段：{exc}") from exc
            fitted = wav_duration(destination)
            if fitted>duration+1/24000+.000001 or (duration<=slot['window'] and abs(fitted-duration)>1/24000+.000001):
                raise ValueError(f"第 {row['id']} 段配音时长被改变；短配音必须保持原速，未替换音轨")
            return index, (slot["start"], destination), dict(id=row["id"], start=slot["start"], end=slot["start"] + fitted)

        placed, timing = [None] * len(clips), [None] * len(clips)
        executor = ThreadPoolExecutor(max_workers=settings.audio_workers, thread_name_prefix="audio-fit")
        futures = [executor.submit(fit_one, index) for index in range(len(clips))]
        last_progress = 0
        try:
            for completed, future in enumerate(as_completed(futures), 1):
                index, clip, record = future.result()
                placed[index], timing[index] = clip, record
                if time.monotonic() - last_progress >= .7 or completed == len(clips):
                    self.progress(identifier, "dub", 55 + round(completed / len(clips) * 35), f"CPU 并行配音对齐 {completed}/{len(clips)}")
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
        target = folder / "chinese.pending.wav"
        mix_timeline(placed, project["media"]["duration"], target, cancel)
        os.replace(target, folder / "chinese.wav")
        signature = dub_signature(self.store.read(identifier), folder, settings)
        self.store.update(identifier, dub_signature=signature, dub_timing=timing, outputs={"audio": "chinese.wav"})
        self.write_subtitles(identifier)
        self.progress(identifier, "dub", 100, "中文音轨生成完成，可试听后导出视频")

    def export(self, identifier, settings, cancel):
        project = self.store.read(identifier)
        folder = self.store.folder(identifier)
        if not project.get("dub_signature") or not (folder / "chinese.wav").exists():
            raise ValueError("请先生成当前版本的中文配音")
        if project["dub_signature"] != dub_signature(project, folder, settings):
            raise ValueError("配音设置、模型或参考声音已变化，请重新生成中文配音后导出")
        self.write_subtitles(identifier)
        export_project(self.store, identifier, settings, cancel, self.progress, offline_env(), before_gpu=lambda: self.tts.release("为背景分离释放显存"))

    def preview(self, identifier, settings, cancel):
        self.write_subtitles(identifier)
        preview_existing(self.store, identifier, settings, cancel, self.progress)
