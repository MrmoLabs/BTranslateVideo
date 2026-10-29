from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, SETTINGS, Settings, atomic_json, diagnostics, load_settings
from .media import extract_audio, probe
from .pipeline import Pipeline
from .export_media import archive_current, invalidate_export, audio_mode, gain
from .translation_proposals import read_proposal, selected_changes
from .store import Store
from .subtitles import parse_srt, validate_segments
from .languages import SOURCE_LANGUAGES


class ImportProject(BaseModel):
    path: str
    name: str = ""
    language: str = "en"
    mode: str = "course"


class SegmentsPayload(BaseModel):
    segments: list[dict] = Field(max_length=30000)
    revision: int | None = None


class ProposalApply(BaseModel):
    proposal_id: str
    revision: int
    ids: list[int] = Field(min_length=1, max_length=30000)

class AuditionPayload(BaseModel):
    segment: dict
    revision: int


class AudioOptions(BaseModel):
    mode: str
    gain: float = Field(default=1.0, ge=0, le=2)



def create_app(data_root: Path | None = None) -> FastAPI:
    store = Store(data_root) if data_root else Store()
    pipeline = Pipeline(store)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        pipeline.close()

    app = FastAPI(title="BTranslateVideo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.pipeline = store, pipeline

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # Local-only service: reject DNS rebinding and cross-site form/JSON mutations.
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站操作"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exc):
        return JSONResponse({"detail": "找不到项目或文件"}, status_code=404)

    def editable(identifier):
        store.read(identifier)
        if pipeline.busy(identifier):
            raise HTTPException(409, "项目处理中，请等当前阶段完成或取消后再编辑")

    @app.get("/api/languages")
    def source_languages():
        return SOURCE_LANGUAGES

    @app.get("/api/settings")
    def settings_get():
        return load_settings().model_dump()

    @app.put("/api/settings")
    def settings_put(settings: Settings):
        atomic_json(SETTINGS, settings.model_dump())
        return settings.model_dump()

    @app.post("/api/shutdown")
    def shutdown():
        callback = getattr(app.state, "request_shutdown", None)
        if callback is None:
            raise HTTPException(409, "此实例不支持远程退出")
        callback()
        return {"shutting_down": True}

    @app.get('/api/tts-session')
    def tts_session():
        return pipeline.tts.status()

    @app.post('/api/tts-session/release')
    def release_tts_session():
        if pipeline.events:
            raise HTTPException(409, '配音模型正在工作，请先完成或取消当前任务')
        try:
            pipeline.tts.release(blocking=False)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return pipeline.tts.status()

    @app.get("/api/diagnostics")
    def environment():
        return diagnostics(load_settings())

    @app.get("/api/projects")
    def projects():
        return store.list()

    @app.get("/api/projects/{identifier}")
    def project(identifier: str):
        data = store.read(identifier)
        log = store.folder(identifier) / "process.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16000))
                data["process_log"] = stream.read().decode("utf-8", errors="replace")
        else:
            data["process_log"] = ""
        return data

    def initialize(path: Path, name, language, mode, upload=None):
        if language not in SOURCE_LANGUAGES or mode not in {"course", "anime"}:
            raise ValueError("无效的语言或内容类型")
        settings = load_settings()
        if upload is None:
            if not path.is_file():
                raise ValueError("本地视频文件不存在")
            info = probe(path, settings)
            if not info["video"] or not info["audio"]:
                raise ValueError("请选择包含画面和音轨的视频")
        data = store.create(name or path.name, language, mode)
        folder = store.folder(data["id"])
        destination = folder / "source.media"
        try:
            if upload is not None:
                with destination.open("wb") as output:
                    shutil.copyfileobj(upload, output, length=4 * 1024 * 1024)
                info = probe(destination, settings)
                if not info["video"] or not info["audio"]:
                    raise ValueError("请选择包含画面和音轨的视频")
            else:
                shutil.copyfile(path, destination)
            return store.update(data["id"], media=info, source_name=path.name)
        except Exception as exc:
            store.update(data["id"], status="failed", error=str(exc))
            raise

    @app.post("/api/projects/import")
    def import_local(data: ImportProject):
        path = Path(data.path).expanduser()
        if not path.is_absolute():
            raise ValueError("请输入视频的完整绝对路径")
        return initialize(path, data.name, data.language, data.mode)

    @app.post("/api/projects/upload")
    def upload_video(video: UploadFile = File(...), language: str = Form("en"), mode: str = Form("course"), name: str = Form("")):
        return initialize(Path(Path(video.filename or "video.mp4").name), name, language, mode, video.file)

    @app.put("/api/projects/{identifier}/segments")
    def save_segments(identifier: str, data: SegmentsPayload):
        with pipeline.lock:
            editable(identifier)
            previous = store.read(identifier)
            if data.revision is not None and data.revision != previous.get("edit_revision", 0):
                raise HTTPException(409, "字幕已被其他操作更新。当前编辑仍在页面中，请先复制需要保留的内容，再刷新项目，避免覆盖新结果。")
            segments = validate_segments(data.segments, previous["media"]["duration"])
            old = {item["id"]: item for item in previous["segments"]}
            for item in segments:
                prior = old.get(item["id"])
                if prior and item["text"] != prior["text"] and item["zh"] == prior["zh"]:
                    item["zh"] = ""
            if segments == previous["segments"]:
                return previous
            archive_current(store, identifier)
            atomic_json(store.folder(identifier) / "segments.previous.json", previous["segments"])
            result = store.update(identifier, segments=segments, dub_signature="", outputs={}, error="", status="idle")
            pipeline.write_subtitles(identifier)
            return result

    @app.get("/api/projects/{identifier}/translation-proposal")
    def translation_proposal(identifier: str):
        return read_proposal(store, identifier)

    @app.post("/api/projects/{identifier}/translation-proposal/apply")
    def apply_translation_proposal(identifier: str, data: ProposalApply):
        with pipeline.lock:
            editable(identifier)
            try:
                segments = selected_changes(store, identifier, data.proposal_id, data.revision, data.ids)
            except ValueError as error:
                raise HTTPException(409, str(error))
            project = store.read(identifier)
            archive_current(store, identifier)
            atomic_json(store.folder(identifier) / "segments.previous.json", project["segments"])
            metadata = {**project["translation_proposal"], "status": "applied", "applied_ids": data.ids}
            result = store.update(identifier, segments=segments, translation_proposal=metadata, outputs={}, dub_signature="", status="idle", error="")
            pipeline.write_subtitles(identifier)
            store.log(identifier, f"已应用 {len(data.ids)} 段译文优化；请重新生成配音和成片，未修改的语音缓存可复用")
            return result

    @app.post("/api/projects/{identifier}/subtitles")
    def import_subtitles(identifier: str, subtitle: UploadFile = File(...), kind: str = Form("source")):
        with pipeline.lock:
            editable(identifier)
            project = store.read(identifier)
            raw = subtitle.file.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("字幕文件超过 8MB")
            try:
                segments = parse_srt(raw.decode("utf-8-sig"), project["media"]["duration"])
            except UnicodeDecodeError:
                raise ValueError("字幕需要使用 UTF-8 编码")
            if kind == "zh":
                existing = project["segments"]
                if len(existing) != len(segments) or any(abs(a["start"] - b["start"]) > .05 or abs(a["end"] - b["end"]) > .05 for a, b in zip(existing, segments)):
                    raise ValueError("中文字幕必须与当前原文字幕的段数和时间轴一致")
                for a, b in zip(existing, segments):
                    a["zh"] = b["text"]
                segments = existing
            elif kind != "source":
                raise ValueError("无效字幕类型")
            atomic_json(store.folder(identifier) / "segments.previous.json", project["segments"])
            result = store.update(identifier, segments=segments, outputs={}, dub_signature="", status="idle", error="")
            pipeline.write_subtitles(identifier)
            return result

    @app.post("/api/projects/{identifier}/reference/{speaker}")
    def reference(identifier: str, speaker: str, audio: UploadFile = File(...)):
        with pipeline.lock:
            editable(identifier)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", speaker):
                raise ValueError("无效角色编号")
            folder = store.folder(identifier)
            source = folder / "reference-upload.media"
            with source.open("wb") as output:
                shutil.copyfileobj(audio.file, output)
            settings = load_settings()
            info = probe(source, settings)
            if not info["audio"] or not 3 <= info["duration"] <= 30:
                source.unlink(missing_ok=True)
                raise ValueError("参考声音需包含音频，长度为 3～30 秒")
            name = f"reference-{speaker}.wav"
            pending = folder / f"reference-{speaker}.pending.wav"
            try:
                extract_audio(source, pending, settings, folder / "process.log", threading.Event())
                os.replace(pending, folder / name)
            finally:
                source.unlink(missing_ok=True)
            refs = store.read(identifier)["references"]
            refs[speaker] = name
            return store.update(identifier, references=refs, outputs={}, dub_signature="")

    @app.put("/api/projects/{identifier}/audio-options")
    def audio_options(identifier: str, data: AudioOptions):
        with pipeline.lock:
            editable(identifier)
            project = store.read(identifier)
            if data.mode not in {"original", "none", "custom"}: raise ValueError("无效背景声音模式")
            if data.mode == "custom" and not project.get("background"): raise ValueError("请先上传自定义背景音")
            if data.mode == audio_mode(project) and data.gain == gain(project): return project
            archive_current(store, identifier)
            return store.update(identifier, background_mode=data.mode, background_gain=data.gain, outputs=invalidate_export(project), export_signature="", preview_info={})

    @app.post("/api/projects/{identifier}/background")
    def background(identifier: str, audio: UploadFile = File(...)):
        with pipeline.lock:
            editable(identifier)
            folder = store.folder(identifier)
            source = folder / "background.pending.media"
            with source.open("wb") as output:
                shutil.copyfileobj(audio.file, output)
            if not probe(source, load_settings())["audio"]:
                raise ValueError("背景文件没有音轨")
            project = store.read(identifier)
            archive_current(store, identifier)
            os.replace(source, folder / "background.media")
            return store.update(identifier, background="background.media", background_mode="custom", background_gain=gain(project) if audio_mode(project) == "custom" else .3, outputs=invalidate_export(project), export_signature="", preview_info={})

    @app.delete("/api/projects/{identifier}/background")
    def remove_background(identifier: str):
        with pipeline.lock:
            editable(identifier)
            project = store.read(identifier)
            archive_current(store, identifier)
            return store.update(identifier, background="", background_mode="none", outputs=invalidate_export(project), export_signature="", preview_info={})

    @app.post('/api/projects/{identifier}/audition')
    def audition(identifier:str,data:AuditionPayload):
        with pipeline.lock:
            editable(identifier);project=store.read(identifier)
            if data.revision!=project.get('edit_revision',0):raise HTTPException(409,'字幕已经更新，请保留当前修改并重新打开项目后试听')
            row=validate_segments([data.segment],project['media']['duration'])[0]
            if row['id'] not in {r['id'] for r in project['segments']}:raise ValueError('请选择当前项目中的句子')
            if not row['zh']:raise ValueError('请先填写这句的中文配音稿')
            request_id=uuid.uuid4().hex[:24]
            store.update(identifier,audition_request=dict(id=request_id,segment=row))
            pipeline.submit(identifier,'audition',load_settings())
            return dict(queued=True,request_id=request_id)

    @app.post("/api/projects/{identifier}/run/{stage}")
    def start(identifier: str, stage: str):
        store.read(identifier)
        pipeline.submit(identifier, stage, load_settings())
        return {"queued": True}

    @app.post("/api/projects/{identifier}/cancel")
    def cancel(identifier: str):
        store.read(identifier)
        pipeline.cancel(identifier)
        return {"cancelled": True}

    @app.get("/api/projects/{identifier}/files/{filename}")
    def file(identifier: str, filename: str, download: bool = False):
        project = store.read(identifier)
        allowed = {"source.media", "chinese.wav", "result.mkv", "original.srt", "chinese.srt", "bilingual.srt", "chinese.vtt", *project["references"].values()}
        outputs = project.get("outputs", {})
        audition_name=project.get('audition',{}).get('file','')
        if re.fullmatch(r'audition-[0-9a-f]{24}\.wav',audition_name):allowed.add(audition_name)
        for kind, pattern in [("preview", r"preview-[0-9a-f]{24}\.mp4"), ("mixed_audio", r"mix-[0-9a-f]{24}\.flac")]:
            name = outputs.get(kind, "")
            if re.fullmatch(pattern, name): allowed.add(name)
        if filename not in allowed:
            raise HTTPException(404, "文件不可访问")
        path = store.folder(identifier) / filename
        if not path.is_file():
            raise HTTPException(404, "尚未生成此文件")
        if filename in {"chinese.wav", "result.mkv"} and filename not in project["outputs"].values():
            raise HTTPException(409, "此输出已过期，请重新生成")
        media_type = "video/mp4" if filename == "source.media" or filename.endswith(".mp4") else "text/vtt" if filename.endswith(".vtt") else None
        return FileResponse(path, media_type=media_type, filename=filename if download else None)

    @app.get("/")
    def home():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
    return app
