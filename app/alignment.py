"""Plan complete utterances with bounded drift and no new voice overlap."""
from __future__ import annotations


def plan_alignment(segments, durations, video_duration, settings):
    if len(segments) != len(durations) or not segments:
        raise ValueError("配音时长与字幕段数不一致")
    if any(d <= 0 for d in durations):
        raise ValueError("配音片段时长必须大于零")
    overlap = any(a["end"] > b["start"] + .00001 for a, b in zip(segments, segments[1:]))
    if overlap:
        # Original overlapping dialogue is intentional. Keep its original mix
        # rather than silently turning it into sequential speech.
        plan = []
        for i, (row, duration) in enumerate(zip(segments, durations)):
            next_start = segments[i + 1]["start"] if i + 1 < len(segments) else video_duration
            end = min(video_duration, max(row["end"], next_start))
            window = end - row["start"]
            if duration / window > settings.max_speed + .00001:
                raise ValueError(f"第 {row['id']} 段原声包含重叠对白，配音仍过长，请缩短译文或调整这段时间")
            plan.append(dict(id=row["id"], start=row["start"], window=window))
        return plan

    latest = [None] * len(segments)
    next_latest_start = video_duration
    for i in range(len(segments) - 1, -1, -1):
        row, duration = segments[i], durations[i]
        normal_end = max(row["end"], segments[i + 1]["start"] if i + 1 < len(segments) else video_duration)
        # Reserve a small filter tolerance without changing the voice or cutting
        # its tail. FFmpeg's measured output decides the final valid duration.
        minimum = duration / settings.max_speed + min(.12, .04 + duration * .002)
        end = min(video_duration, normal_end + settings.max_lag, next_latest_start)
        start = min(row["start"] + settings.max_lag, end - minimum)
        if start < max(0, row["start"] - settings.max_lead) - .00001:
            raise ValueError(f"第 {row['id']} 段及相邻配音无法在允许的时间偏移和 {settings.max_speed:.2f} 倍速内完整容纳，请缩短译文或调整这几段时间；系统不会截断配音")
        latest[i] = (start, end, minimum)
        next_latest_start = start

    result, previous_end = [], 0
    for i, (row, duration, (latest_start, latest_end, minimum)) in enumerate(zip(segments, durations, latest)):
        start = max(previous_end, 0, row["start"] - settings.max_lead, min(row["start"], latest_start))
        normal_end = max(row["end"], segments[i + 1]["start"] if i + 1 < len(segments) else video_duration)
        # Prefer the original cue end. Borrow only the extra time actually
        # needed, leaving the next utterance its proven minimum window.
        window = min(latest_end - start, max(normal_end - start, minimum))
        if window <= 0 or duration / window > settings.max_speed + .00001:
            raise ValueError(f"第 {row['id']} 段没有足够的完整配音窗口，系统不会截断配音")
        result.append(dict(id=row["id"], start=start, window=window))
        previous_end = start + min(duration, window)
    return result
