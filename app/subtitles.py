from __future__ import annotations

import math
import re
from .emotions import emotion_fields


def seconds(value: str) -> float:
    match = re.fullmatch(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if not match:
        raise ValueError(f"无效字幕时间：{value}")
    hours, minutes, secs, millis = map(int, match.groups())
    if minutes > 59 or secs > 59:
        raise ValueError(f"无效字幕时间：{value}")
    return hours * 3600 + minutes * 60 + secs + millis / 1000


def timestamp(value: float) -> str:
    millis = round(max(0, value) * 1000)
    secs, millis = divmod(millis, 1000)
    minutes, secs = divmod(secs, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def validate_segments(segments: list[dict], duration: float | None = None) -> list[dict]:
    if not segments:
        raise ValueError("没有可处理的字幕")
    if len(segments) > 30000:
        raise ValueError("字幕超过 30000 段，请分割视频")
    output = []
    seen = set()
    last_start = -1
    for i, item in enumerate(segments):
        start, end = float(item["start"]), float(item["end"])
        identifier = int(item.get("id", i + 1))
        if identifier <= 0 or identifier in seen:
            raise ValueError("字幕编号必须唯一且为正整数")
        if not all(map(math.isfinite, [start, end])) or start < 0 or end <= start:
            raise ValueError(f"第 {identifier} 段时间无效")
        if start < last_start or (duration is not None and end > duration + .1):
            raise ValueError(f"第 {identifier} 段顺序错误或超出视频时长")
        speaker = str(item.get("speaker", "speaker_1")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", speaker):
            raise ValueError("角色编号只能包含字母、数字、下划线和连字符")
        text, zh = str(item.get("text", "")).strip(), str(item.get("zh", "")).strip()
        if not text or len(text) > 6000 or len(zh) > 6000:
            raise ValueError(f"第 {identifier} 段原文为空或文本过长")
        output.append(dict(id=identifier, start=start, end=end, text=text, zh=zh, speaker=speaker,**emotion_fields(item)))
        seen.add(identifier)
        last_start = start
    return output


def parse_srt(text: str, duration: float | None = None) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip())
    output = []
    for block in blocks:
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines or "-->" not in lines[0]:
            raise ValueError("请导入带起止时间的 SRT 字幕")
        start, end = lines.pop(0).split("-->", 1)
        output.append(dict(id=len(output) + 1, start=seconds(start), end=seconds(end.split()[0]), text="\n".join(lines), zh="", speaker="speaker_1"))
    return validate_segments(output, duration)


def render_srt(segments: list[dict], language: str = "zh", bilingual: bool = False) -> str:
    blocks = []
    for i, segment in enumerate(segments, 1):
        text = segment.get(language, "").strip()
        if bilingual:
            text = text + "\n" + segment["text"]
        blocks.append(f"{i}\n{timestamp(segment['start'])} --> {timestamp(segment['end'])}\n{text}\n")
    return "\n".join(blocks)


def merge_short_segments(segments: list[dict], max_span: float = 12) -> list[dict]:
    output = []
    for item in segments:
        item = dict(item)
        if output:
            prev = output[-1]
            gap = item["start"] - prev["end"]
            if prev["speaker"] == item["speaker"] and 0 <= gap < .45 and item["end"] - prev["start"] <= max_span and (prev["end"] - prev["start"] < 2.5 or not re.search(r"[.!?。！？][\"'”’]?\s*$", prev["text"])):
                prev["text"] += " " + item["text"]
                prev["end"] = item["end"]
                continue
        output.append(item)
    for i, item in enumerate(output, 1):
        item["id"] = i
    return output


def render_vtt(segments):
    import html
    blocks = ["WEBVTT\n"]
    for segment in segments:
        if segment.get("zh"):
            start, end = timestamp(segment["start"]).replace(",", "."), timestamp(segment["end"]).replace(",", ".")
            blocks.append(f"{start} --> {end}\n{html.escape(segment['zh'])}\n")
    return "\n".join(blocks)
