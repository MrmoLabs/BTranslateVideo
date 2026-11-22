from __future__ import annotations

import json
import asyncio
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import httpx

from .config import BILIBILI_TRANSLATION_URL, ROOT, Settings, llama_path, resolve
from .media import check_cancel, spawn, stop_process
from .model_backends import uses_translation_service


@contextmanager
def _local_translator_unlocked(settings: Settings, log: Path, cancel: Event):
    if uses_translation_service(settings):
        yield settings.translation_service_url
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
    if uses_translation_service(settings):
        yield settings.translation_service_url
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


class TranslationIDsError(ValueError):
    """An exact identity mismatch; callers may retry a smaller request."""


def translation_items(content: str, *, allow_id_map: bool = False) -> list[dict]:
    content = content.strip()
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise TranslationIDsError('译文 JSON 出现重复字段，未保存此批次')
        return result

    value = json.loads(content, object_pairs_hook=unique_keys if allow_id_map else None)
    if allow_id_map and isinstance(value, dict) and value and all(key.isascii() and key.isdigit() for key in value):
        return [dict(id=key, translation=text) for key, text in value.items()]
    items = value.get("translations") if isinstance(value, dict) else value
    if not isinstance(items, list):
        raise ValueError("翻译模型未返回 translations 数组")
    return items


def parse_translation(content: str, expected: list[int], *, allow_id_map: bool = False) -> dict[int, str]:
    result = {}
    for item in translation_items(content, allow_id_map=allow_id_map):
        if not isinstance(item, dict):
            raise ValueError("译文条目必须包含 id 和译文")
        raw_id = item.get("id")
        if type(raw_id) is int:
            identifier = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isascii() and raw_id.strip().isdigit():
            identifier = int(raw_id)
        else:
            raise TranslationIDsError("译文编号必须是整数，未保存此批次")
        text = item.get("translation", item.get("zh"))
        if text is None and allow_id_map:
            text = item.get("text")
        if identifier in result:
            raise TranslationIDsError(f"译文编号重复：{identifier}，未保存此批次")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("译文内容为空")
        result[identifier] = text.strip()
    if set(result) != set(expected):
        missing = sorted(set(expected) - set(result))
        extra = sorted(set(result) - set(expected))
        raise TranslationIDsError(f"译文编号与原文不匹配，未保存此批次；缺少编号 {missing[:12]}，多余编号 {extra[:12]}；要求编号 {expected[:12]}")
    return result


async def _post_cancellable(url, payload, cancel, *, read_timeout=180):
    bilibili = url == BILIBILI_TRANSLATION_URL + '/chat/completions'
    headers = {'User-Agent': 'Index-Translate-Client/1.0'} if bilibili else None
    async with httpx.AsyncClient(trust_env=bilibili, follow_redirects=False, headers=headers,
                                 timeout=httpx.Timeout(read_timeout, connect=5)) as client:
        task = asyncio.create_task(client.post(url, json=payload))
        try:
            while not task.done():
                check_cancel(cancel)
                await asyncio.wait({task}, timeout=.2)
            response = await task
            if bilibili and not response.is_success:
                raise RuntimeError(f'B 站官方翻译 API 请求失败（HTTP {response.status_code}）')
            return response
        except httpx.RequestError as error:
            if bilibili:
                raise RuntimeError('无法连接 B 站官方翻译 API，请检查网络后重试') from error
            raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def translate_batch(url, settings, batch, context, mode, cancel, *, cache=None, topic=None, on_checks=None, on_failure=None):
    from .translation_quality import run_workflow
    result = run_workflow(url, settings, batch, context, mode, cancel, cache=cache, topic=topic, on_failure=on_failure)
    if on_checks is not None:
        on_checks({identifier:item["checks"] for identifier,item in result.items() if item.get("checks")})
    return {identifier: item["zh"] for identifier, item in result.items()}
