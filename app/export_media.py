"""Background-preserving exports and browser-safe Chinese MP4 previews."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .config import ROOT, atomic_json, executable, resolve, separation_python
from .media import check_cancel, ffmpeg, probe, run


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024): result.update(chunk)
    return result.hexdigest()


def audio_mode(project):
    return project.get("background_mode", "custom" if project.get("background") else "none")


def gain(project):
    return float(project.get("background_gain", .3 if audio_mode(project) == "custom" else 1))


def invalidate_export(project):
    return {k: v for k, v in project.get("outputs", {}).items() if k not in {"video", "preview", "mixed_audio"}}


def archive_current(store, identifier):
    project = store.read(identifier); folder = store.folder(identifier)
    names = {"result.mkv", "bilingual.srt", "chinese.srt", *project.get("outputs", {}).values()}
    if project.get("background"): names.add(project["background"])
    if not (folder / "result.mkv").exists(): return
    target = folder / "history" / str(time.time_ns()); target.mkdir(parents=True)
    atomic_json(target / "project.json", project)
    for name in names:
        source = folder / name
        if source.is_file() and source.parent == folder:
            if source.suffix in {".srt", ".vtt"}: shutil.copy2(source, target / name)
            else:
                try: os.link(source, target / name)
                except OSError: shutil.copy2(source, target / name)


def original_background(store, identifier, settings, cancel, progress, env, before_gpu=None):
    project = store.read(identifier); folder = store.folder(identifier)
    model = resolve(settings.separation_model)
    if not separation_python().is_file() or not all((model / name).is_file() for name in ["manifest.json", "htdemucs.safetensors", "config.json"]):
        raise ValueError("原背景音分离模型尚未准备，请运行 setup.ps1，或选择仅中文配音/自定义背景音")
    manifest = json.loads((model / "manifest.json").read_text(encoding="utf-8"))
    identity = key(dict(schema=1, source=sha(folder / "source.media"), model=manifest["weights_sha256"], segment=6, overlap=.25, shifts=0))
    output = folder / "cache" / f"background-{identity}.flac"
    if output.is_file():
        store.log(identifier, "复用已分离的原片背景音")
        return output, identity
    progress(identifier, "background", 10, "本地分离原对白，保留音乐、环境声与音效；按 30 秒分块处理")
    request_path = folder / "cache" / "background-request.json"
    progress_path = folder / "cache" / "background-progress.json"
    atomic_json(request_path, dict(model=str(model), source=str(folder / "source.media"), output=str(output), duration=project["media"]["duration"], ffmpeg=executable(settings.ffmpeg), progress=str(progress_path)))
    previous = -1
    def tick():
        nonlocal previous
        if progress_path.is_file():
            try: data = json.loads(progress_path.read_text(encoding="utf-8"))
            except (ValueError, OSError): return
            percent = round(40 * data["frames"] / max(data["total"], 1))
            if percent != previous:
                previous = percent
                progress(identifier, "background", 10 + percent, f"保留原背景音：已处理 {data['frames'] / 44100:.0f}/{project['media']['duration']:.0f} 秒")
    progress_path.unlink(missing_ok=True)
    if before_gpu:
        before_gpu()
    run([separation_python(), ROOT / "scripts" / "separate_worker.py", request_path], folder / "process.log", cancel, env=env, timeout=86400, on_tick=tick)
    check_cancel(cancel)
    if abs(probe(output, settings)["duration"] - project["media"]["duration"]) > .1:
        raise ValueError("分离背景音时长不匹配，未用于导出")
    return output, identity


def make_preview(store, identifier, source, settings, cancel, progress, signature=None, force=False):
    folder = store.folder(identifier); project = store.read(identifier)
    identity = key(dict(schema=1, export=signature or sha(source), profile="h264-720p-aac", encoder=settings.preview_encoder))
    name = f"preview-{identity}.mp4"; output = folder / name
    if output.is_file() and not force: return name, dict(signature=identity, cached=True)
    info = probe(source, settings)
    video = next(x for x in info["streams"] if x["codec_type"] == "video")
    ratio = min(1, 1280 / video["width"], 720 / video["height"])
    width, height = max(2, int(video["width"] * ratio) // 2 * 2), max(2, int(video["height"] * ratio) // 2 * 2)
    attempts = ["cpu"] if settings.preview_encoder == "cpu" else ["cuda", "nvenc", "cpu"]
    pending = folder / f"preview-{identity}.pending.mp4"
    for encoder in attempts:
        check_cancel(cancel)
        progress(identifier, "preview", 70, "生成可在页面播放的中文 MP4 预览")
        prefix = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if encoder == "cuda" else []
        filters = f"scale_cuda={width}:{height}:format=yuv420p" if encoder == "cuda" else f"scale={width}:{height},format=yuv420p"
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"] if encoder == "cpu" else ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "25", "-b:v", "0"]
        track = "0:a:1" if len([x for x in info["streams"] if x["codec_type"] == "audio"]) > 1 else "0:a:0"
        progress_path = folder / "cache" / f"preview-{encoder}.progress"
        progress_path.unlink(missing_ok=True)
        previous = -1
        def tick():
            nonlocal previous
            if progress_path.is_file():
                try:
                    values = dict(line.split("=", 1) for line in progress_path.read_text(encoding="utf-8").splitlines() if "=" in line)
                    seconds = int(values.get("out_time_us", 0)) / 1000000
                except (OSError, ValueError): return
                percent = min(98, 70 + round(28 * seconds / max(project["media"]["duration"], 1)))
                if percent != previous:
                    previous = percent
                    progress(identifier, "preview", percent, f"页面预览：{seconds:.0f}/{project['media']['duration']:.0f} 秒")
        arguments = [executable(settings.ffmpeg), "-hide_banner", "-nostdin", "-y", *prefix, "-i", str(source), "-map", "0:v:0", "-map", track, "-sn", "-dn", "-vf", filters, *codec, "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000", "-movflags", "+faststart", "-progress", str(progress_path), "-nostats", str(pending)]
        try:
            run(arguments, folder / "process.log", cancel, on_tick=tick, timeout=86400)
            preview = probe(pending, settings)
            if abs(preview["duration"] - info["duration"]) > .2: raise ValueError("预览时长不匹配")
            if len([x for x in preview["streams"] if x["codec_type"] == "audio"]) != 1: raise ValueError("页面预览必须只有中文音轨")
            os.replace(pending, output)
            return name, dict(signature=identity, encoder=encoder, width=width, height=height, generated_at=time.time_ns())
        except RuntimeError:
            check_cancel(cancel)
            if encoder == "cpu": raise
            store.log(identifier, "硬件预览编码不可用，自动尝试兼容编码方式")
    raise RuntimeError("无法生成页面预览")


def preview_existing(store, identifier, settings, cancel, progress):
    project = store.read(identifier); folder = store.folder(identifier)
    if not project.get("outputs", {}).get("video") or not (folder / "result.mkv").is_file():
        raise ValueError("请先导出当前版本的视频")
    name, info = make_preview(store, identifier, folder / "result.mkv", settings, cancel, progress, project.get("export_signature"), force=True)
    check_cancel(cancel)
    store.update(identifier, outputs={**project["outputs"], "preview": name}, preview_info=info)
    progress(identifier, "preview", 100, "页面预览已准备，可以直接播放")


def export_project(store, identifier, settings, cancel, progress, env, before_gpu=None):
    project = store.read(identifier); folder = store.folder(identifier)
    mode = audio_mode(project); bed, bed_identity = None, "none"
    if mode == "original": bed, bed_identity = original_background(store, identifier, settings, cancel, progress, env, before_gpu=before_gpu)
    elif mode == "custom":
        bed = folder / project.get("background", "")
        if not bed.is_file(): raise ValueError("请先上传不含对白的自定义背景音")
        bed_identity = sha(bed)
    elif mode != "none": raise ValueError("无效背景声音模式")
    signature = key(dict(schema=1, dub=project["dub_signature"], source=sha(folder / "source.media"), mode=mode, bed=bed_identity, gain=gain(project)))
    audio = folder / "chinese.wav"
    mixed = None
    if bed is not None:
        mixed = folder / f"mix-{signature}.flac"
        if not mixed.is_file():
            progress(identifier, "export", 55, "混合中文对白与背景音，保持背景立体声与原时间轴")
            temporary = folder / f"mix-{signature}.pending.flac"
            ffmpeg(settings, ["-i", str(audio), "-i", str(bed), "-filter_complex", f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[voice];[1:a]volume={gain(project):.5f}[bed];[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1[a]", "-map", "[a]", "-c:a", "flac", str(temporary)], folder / "process.log", cancel)
            os.replace(temporary, mixed)
        audio = mixed
    progress(identifier, "export", 60, "封装原画面、原音轨、含背景中文音轨与双语字幕")
    temporary = folder / f"result-{signature}.pending.mkv"
    ffmpeg(settings, ["-i", str(folder / "source.media"), "-i", str(audio), "-i", str(folder / "bilingual.srt"), "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0", "-map", "2:s:0", "-map_metadata", "0", "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "aac", "-b:a:1", "160k", "-c:s", "srt", "-metadata:s:a:0", "title=原始音轨", "-metadata:s:a:0", "language=" + {"en": "eng", "ja": "jpn"}.get(project["language"], "und"), "-metadata:s:a:1", "title=中文配音与背景音" if bed else "title=中文配音", "-metadata:s:a:1", "language=zho", "-disposition:a:0", "0", "-disposition:a:1", "default", "-metadata:s:s:0", "title=双语字幕", "-metadata:s:s:0", "language=zho", "-disposition:s:0", "default", str(temporary)], folder / "process.log", cancel)
    info = probe(temporary, settings)
    if len([x for x in info["streams"] if x["codec_type"] == "audio"]) != 2: raise ValueError("导出结果缺少双音轨")
    name, preview = make_preview(store, identifier, temporary, settings, cancel, progress, signature)
    check_cancel(cancel)
    archive_current(store, identifier)
    os.replace(temporary, folder / "result.mkv")
    outputs = dict(audio="chinese.wav", video="result.mkv", preview=name, subtitles="bilingual.srt", chinese_subtitles="chinese.srt")
    if mixed: outputs["mixed_audio"] = mixed.name
    store.update(identifier, outputs=outputs, export_signature=signature, preview_info=preview, export_info=dict(background_mode=mode, background_gain=gain(project), background_model=bed_identity))
    progress(identifier, "export", 100, "中文成片与页面预览已完成")
