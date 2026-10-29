from __future__ import annotations

import json
import os
import subprocess
import shutil
import time
import wave
from pathlib import Path
from threading import Event

from .config import Settings, executable


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel: Event):
    if cancel.is_set():
        raise Cancelled("任务已取消，已完成的片段保留在本地")


def stop_process(process: subprocess.Popen):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)


def spawn(args, **kwargs):
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen([str(arg) for arg in args], **kwargs)


def run(args, logfile: Path, cancel: Event, cwd: Path | None = None, env=None, timeout=7200, on_tick=None, poll_interval=.2):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with logfile.open("ab") as output:
        output.write(b"\n--- process start ---\n")
        output.flush()
        log_start = logfile.stat().st_size
        process = spawn(args, stdout=output, stderr=subprocess.STDOUT, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
        last_tick = 0
        try:
            while process.poll() is None:
                check_cancel(cancel)
                if time.monotonic() - started > timeout:
                    raise RuntimeError("处理超时，请检查日志后重试")
                if on_tick and time.monotonic() - last_tick > .75:
                    on_tick()
                    last_tick = time.monotonic()
                cancel.wait(poll_interval)
            if process.returncode:
                with logfile.open("rb") as stream:
                    stream.seek(max(log_start, logfile.stat().st_size - 3000))
                    tail = stream.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"处理程序退出 ({process.returncode})：\n{tail}")
            if on_tick:
                on_tick()
        finally:
            stop_process(process)


def probe(path: Path, settings: Settings) -> dict:
    output = subprocess.run([executable(settings.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if output.returncode:
        raise ValueError("无法读取媒体文件：" + output.stderr.decode("utf-8", errors="replace")[-800:])
    value = json.loads(output.stdout)
    streams = value.get("streams", [])
    duration = float(value.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError("无法取得媒体时长")
    return dict(duration=duration, video=any(s["codec_type"] == "video" for s in streams), audio=any(s["codec_type"] == "audio" for s in streams), streams=streams)


def ffmpeg(settings: Settings, arguments, log: Path, cancel: Event):
    run([executable(settings.ffmpeg), "-hide_banner", "-nostdin", "-y", *arguments], log, cancel, poll_interval=.05)


def extract_audio(source: Path, destination: Path, settings: Settings, log: Path, cancel: Event, start=None, duration=None, sample_rate=24000):
    args = []
    if start is not None:
        args += ["-ss", str(start)]
    args += ["-i", str(source)]
    if duration is not None:
        args += ["-t", str(duration)]
    args += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(destination)]
    ffmpeg(settings, args, log, cancel)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def fit_clip(source: Path, destination: Path, window: float, settings: Settings, log: Path, cancel: Event) -> float:
    if window <= 0:
        raise ValueError("配音窗口必须大于零")
    ready = False
    try:
        with wave.open(str(source), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
            ready = (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (24000, 1, 2)
    except (wave.Error, EOFError):
        duration = probe(source, settings)["duration"]
    speed = max(1.0, duration / window)
    if speed == 1.0:
        check_cancel(cancel)
        if ready:
            shutil.copyfile(source, destination)
        else:
            # A short utterance is never stretched to fill its subtitle window.
            # Resample only; even atempo=1 can alter the original waveform/tail.
            ffmpeg(settings, ["-i", str(source), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-threads", "1", str(destination)], log, cancel)
        if abs(wav_duration(destination)-duration)>1/24000+.000001:
            raise ValueError("原速转换改变了配音时长，未采用该片段")
        return speed
    if speed > settings.max_speed + .005:
        raise ValueError(f"配音长 {duration:.1f} 秒，窗口仅 {window:.1f} 秒，需 {speed:.2f} 倍速。请缩短这段中文或调整时间；系统不会截断配音。")
    for attempt in range(4):
        if speed > settings.max_speed + .00001:
            break
        ffmpeg(settings, ["-i", str(source), "-af", f"atempo={speed:.8f}", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-threads", "1", str(destination)], log, cancel)
        fitted = wav_duration(destination)
        if fitted <= window + 1 / 24000:
            return speed
        # The filter may deviate by several milliseconds. Measure the produced
        # waveform and retry within the speed limit, instead of trimming its tail.
        speed *= (fitted + .025) / window
    raise ValueError("配音对齐仍超出窗口，请稍微缩短译文或增加相邻空白")


def mix_timeline(clips: list[tuple[float, Path]], duration: float, destination: Path, cancel: Event):
    import numpy as np

    rate = 24000
    frames = round(duration * rate)
    scratch = destination.with_suffix(".mix.tmp")
    track = np.memmap(scratch, dtype=np.float32, mode="w+", shape=(frames,))
    try:
        track[:] = 0
        for start, clip in clips:
            check_cancel(cancel)
            with wave.open(str(clip), "rb") as stream:
                if stream.getframerate() != rate or stream.getsampwidth() != 2 or stream.getnchannels() != 1:
                    raise ValueError("内部配音片段格式不正确")
                offset = round(start * rate)
                data = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32)
            if offset + len(data) > frames + 1:
                raise ValueError("配音超出视频结尾，请调整最后一句")
            length = min(len(data), frames - offset)
            track[offset:offset + length] += data[:length]
        peak = 1.0
        for offset in range(0, frames, rate * 10):
            check_cancel(cancel)
            peak = max(peak, float(np.max(np.abs(track[offset:offset + rate * 10]))))
        gain = min(1, 31000 / peak)
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            for offset in range(0, frames, rate * 10):
                check_cancel(cancel)
                data = np.clip(track[offset:offset + rate * 10] * gain, -32768, 32767).astype("<i2")
                output.writeframes(data.tobytes())
    finally:
        track.flush()
        del track
        scratch.unlink(missing_ok=True)
