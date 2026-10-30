from __future__ import annotations

import json
import asyncio
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import httpx

from .config import ROOT, Settings, llama_path, resolve
from .media import check_cancel, spawn, stop_process


@contextmanager
def _local_translator_unlocked(settings: Settings, log: Path, cancel: Event):
    if settings.translation_url:
        yield settings.translation_url
        return
    binary, model = llama_path(settings), resolve(settings.translation_model)
    if not binary or not binary.is_file() or not model.is_file():
        raise RuntimeError("本地翻译模型尚未准备好，请运行 setup.ps1 或在设置中指定本机模型服务")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with log.open("ab") as output:
        process = spawn([binary, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-ngl", "99", "-c", "8192", "--parallel", "1", "--jinja", "--alias", settings.translation_name], cwd=binary.parent, stdout=output, stderr=output, stdin=-3)
        url = f"http://127.0.0.1:{port}/v1"
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                started = time.monotonic()
                while True:
                    check_cancel(cancel)
                    if process.poll() is not None:
                        raise RuntimeError("本地翻译服务启动失败，请查看运行日志")
                    try:
                        if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() - started > 180:
                        raise RuntimeError("本地翻译模型加载超过 3 分钟，请检查日志和显存")
                    cancel.wait(.5)
            yield url
        finally:
            stop_process(process)


@contextmanager
def local_translator(settings: Settings, log: Path, cancel: Event):
    if settings.translation_url:
        yield settings.translation_url
        return
    from filelock import FileLock, Timeout
    lock = FileLock(ROOT / "runtime" / "gpu-model.lock")
    while True:
        check_cancel(cancel)
        try:
            lock.acquire(timeout=.2)
            break
        except Timeout:
            continue
    try:
        with _local_translator_unlocked(settings, log, cancel) as url:
            yield url
    finally:
        lock.release()


def parse_translation(content: str, expected: list[int]) -> dict[int, str]:
    content = content.strip()
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(content)
    items = value.get("translations") if isinstance(value, dict) else value
    if not isinstance(items, list):
        raise ValueError("翻译模型未返回 translations 数组")
    result = {}
    for item in items:
        identifier, text = int(item["id"]), item.get("translation", item.get("zh"))
        if identifier in result or not isinstance(text, str) or not text.strip():
            raise ValueError("译文编号重复或内容为空")
        result[identifier] = text.strip()
    if set(result) != set(expected):
        raise ValueError("译文编号与原文不匹配，未保存此批次")
    return result


async def _post_cancellable(url, payload, cancel):
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(180, connect=5)) as client:
        task = asyncio.create_task(client.post(url, json=payload))
        try:
            while not task.done():
                check_cancel(cancel)
                await asyncio.wait({task}, timeout=.2)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def translate_batch(url, settings, batch, context, mode, cancel, *, cache=None, topic=None):
    from .translation_quality import run_workflow
    result = run_workflow(url, settings, batch, context, mode, cancel, cache=cache, topic=topic)
    return {identifier: item["zh"] for identifier, item in result.items()}
