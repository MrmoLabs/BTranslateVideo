# -*- coding: utf-8 -*-
"""Download Seed-VC inference models through hf-mirror with ranged parallel GETs.

Uses the repository tree API for sizes instead of per-file HEAD requests, which
hf-mirror rate-limits aggressively.
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1] / "vendor" / "seed-vc" / "local-models"
MIRROR = os.environ.get("SEED_VC_MIRROR", "https://hf-mirror.com")
THREADS = int(os.environ.get("SEED_VC_THREADS", "12"))

FILES = [
    ("Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth", "seed-vc/DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth"),
    ("Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_small_wavenet.yml", "seed-vc/config_dit_mel_seed_uvit_whisper_small_wavenet.yml"),
    ("funasr/campplus", "campplus_cn_common.bin", "campplus/campplus_cn_common.bin"),
    ("openai/whisper-small", "config.json", "whisper-small/config.json"),
    ("openai/whisper-small", "preprocessor_config.json", "whisper-small/preprocessor_config.json"),
    ("openai/whisper-small", "model.safetensors", "whisper-small/model.safetensors"),
    ("nvidia/bigvgan_v2_22khz_80band_256x", "config.json", "bigvgan-22k/config.json"),
    ("nvidia/bigvgan_v2_22khz_80band_256x", "bigvgan_generator.pt", "bigvgan-22k/bigvgan_generator.pt"),
]


def sizes(repo: str) -> dict[str, int]:
    for attempt in range(5):
        response = requests.get(f"{MIRROR}/api/models/{repo}/tree/main", timeout=90)
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        response.raise_for_status()
        return {item["path"]: int(item.get("size") or 0) for item in response.json() if item.get("type") == "file"}
    raise RuntimeError(f"无法读取仓库文件列表：{repo}")


def get_bytes(url: str, start: int, end: int, part: Path):
    want = end - start + 1
    if part.is_file() and part.stat().st_size == want:
        return
    for attempt in range(4):
        try:
            with requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=900) as response:
                if response.status_code == 429:
                    raise RuntimeError("rate limited")
                if response.status_code != 206:
                    raise RuntimeError(f"范围请求返回 {response.status_code}")
                with part.open("wb") as out:
                    for chunk in response.iter_content(1024 * 256):
                        out.write(chunk)
            if part.stat().st_size == want:
                return
        except (requests.RequestException, RuntimeError) as exc:
            if attempt == 3:
                raise
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"分段下载失败：{part.name}")


def download(repo: str, remote: str, relative: str, known: dict[str, int]):
    destination = BASE / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"{MIRROR}/{repo}/resolve/main/{remote}"
    size = known.get(remote, 0)
    if size <= 0:
        raise RuntimeError(f"未知文件大小：{repo}/{remote}")
    if destination.is_file() and destination.stat().st_size == size:
        print(f"skip {relative} ({size / 1024**2:.1f} MB)", flush=True)
        return
    for stale in destination.parent.glob(f"{destination.name}.*.part"):
        stale.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    started = time.time()
    print(f"start {relative} ({size / 1024**2:.1f} MB)", flush=True)
    if size <= 8 * 1024**2:
        response = requests.get(url, timeout=300)
        response.raise_for_status()
        destination.write_bytes(response.content)
    else:
        count = THREADS
        chunk = (size + count - 1) // count
        ranges = [(index, index * chunk, min(size - 1, (index + 1) * chunk - 1))
                  for index in range(count) if index * chunk < size]
        with ThreadPoolExecutor(len(ranges)) as pool:
            list(pool.map(lambda args: get_bytes(url, args[1], args[2],
                                                  destination.parent / f"{destination.name}.{args[0]}.part"),
                          ranges))
        pending = destination.with_suffix(destination.suffix + ".part")
        with pending.open("wb") as out:
            for index, _, _ in ranges:
                out.write((destination.parent / f"{destination.name}.{index}.part").read_bytes())
        os.replace(pending, destination)
        for index, _, _ in ranges:
            (destination.parent / f"{destination.name}.{index}.part").unlink(missing_ok=True)
    elapsed = max(.1, time.time() - started)
    print(f"done  {relative} ({size / 1024**2:.1f} MB, {size / 1024 / elapsed:.0f} KB/s)", flush=True)


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    cache = {}
    for repo, remote, relative in FILES:
        try:
            if repo not in cache:
                cache[repo] = sizes(repo)
            download(repo, remote, relative, cache[repo])
        except Exception as exc:  # keep going; report at the end
            print(f"FAIL {relative}: {exc}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
