"""One-time online preparation. Inference uses local_files_only/offline mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import atomic_json


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "BTranslateVideo/0.1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def download_stream(url, headers):
    proxy = os.environ.get("BTV_DOWNLOAD_PROXY")
    if proxy:
        import httpx
        with httpx.Client(proxy=proxy, follow_redirects=True, trust_env=False, timeout=60) as client:
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                yield response.status_code, response.headers, response.iter_bytes(1024 * 1024)
    else:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            yield response.status, response.headers, iter(lambda: response.read(1024 * 1024), b"")


def download(url: str, path: Path, sha256: str | None = None):
    if path.is_file() and path.stat().st_size > 0:
        if sha256 is None or digest(path) == sha256:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    last_error = None
    for attempt in range(4):
        try:
            offset = temporary.stat().st_size if temporary.exists() else 0
            headers = {"User-Agent": "BTranslateVideo/0.1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            with download_stream(url, headers) as (status, response_headers, chunks):
                append = status == 206 and offset > 0
                if not append:
                    offset = 0
                total = int(response_headers.get("Content-Length", 0)) + offset
                written = offset
                last_report = time.monotonic()
                with temporary.open("ab" if append else "wb") as output:
                    for chunk in chunks:
                        output.write(chunk)
                        written += len(chunk)
                        if time.monotonic() - last_report > 5:
                            print(f"{path.name}: {written / 1024**2:.0f} / {total / 1024**2:.0f} MB", flush=True)
                            last_report = time.monotonic()
                if total and written != total:
                    raise IOError("Incomplete download")
            if sha256 and digest(temporary) != sha256:
                temporary.unlink(missing_ok=True)
                raise IOError("SHA-256 mismatch")
            os.replace(temporary, path)
            return
        except Exception as exc:
            last_error = exc
            print(f"Download retry {attempt + 1}: {exc}", flush=True)
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"Download failed: {url}: {last_error}")


def extract(archive: Path, target: Path, strip_root=False):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            parts = Path(entry.filename).parts[1:] if strip_root else Path(entry.filename).parts
            if not parts:
                continue
            destination = (target / Path(*parts)).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError("Unsafe archive path")
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as inp, destination.open("wb") as out:
                    shutil.copyfileobj(inp, out)


def install_source(state):
    destination = ROOT / "vendor" / "index-tts"
    if not (destination / "pyproject.toml").exists():
        revision = "d9e41aac89fd00b3d71497fddb287b7f24613712"
        tree = get_json(f"https://api.github.com/repos/index-tts/index-tts/git/trees/{revision}?recursive=1")
        archive = ROOT / "runtime" / f"index-source-{revision}.zip"
        download(f"https://codeload.github.com/index-tts/index-tts/zip/{revision}", archive)
        extract(archive, destination, strip_root=True)
        for entry in tree["tree"]:
            if entry["type"] == "blob" and entry["path"].startswith("indextts/"):
                data = (destination / entry["path"]).read_bytes()
                actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                if actual != entry["sha"]:
                    raise RuntimeError("Source verification failed: " + entry["path"])
        state["index_source_revision"] = revision
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found")
    print("Preparing separate IndexTTS Python environment", flush=True)
    environment_python = destination / ".venv" / "Scripts" / "python.exe"
    ready = False
    if environment_python.is_file():
        result = subprocess.run([environment_python, "-c", "import torch,torchaudio,indextts; assert torch.version.cuda and torch.__version__.startswith('2.8.'); print('CUDA runtime ready')"], capture_output=True, timeout=45)
        ready = result.returncode == 0
    if not ready:
        subprocess.run([uv, "sync", "--python", "3.11", "--no-sources", "--find-links", "https://download.pytorch.org/whl/cu128", "--default-index", "https://pypi.org/simple"], cwd=destination, check=True)
        subprocess.run([uv, "pip", "install", "--python", str(environment_python), "torch==2.8.0+cu128", "torchaudio==2.8.0+cu128", "--find-links", "https://download.pytorch.org/whl/cu128", "--index-url", "https://pypi.org/simple"], check=True)


def install_llama(state):
    target = ROOT / "runtime" / "llama"
    if list(target.rglob("llama-server.exe")):
        return
    asset = None
    for page in range(1, 10):
        releases = get_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=1&page={page}")
        if not releases:
            break
        release = releases[0]
        asset = next((a for a in release["assets"] if a["name"].endswith("bin-win-vulkan-x64.zip")), None)
        if asset:
            state["llama_release"] = release["tag_name"]
            break
    if not asset:
        raise RuntimeError("No official Windows Vulkan llama.cpp release asset found")
    checksum = asset.get("digest", "")
    archive = ROOT / "runtime" / asset["name"]
    download(asset["browser_download_url"], archive, checksum.removeprefix("sha256:") or None)
    extract(archive, target)
    if not list(target.rglob("llama-server.exe")):
        raise RuntimeError("llama-server.exe missing from archive")


def ms_snapshot(repo, destination, state, include=None):
    from urllib.parse import quote
    listing = get_json(f"https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true")
    if listing.get("Code") != 200:
        raise RuntimeError(f"ModelScope model not found: {repo}")
    files = [f for f in listing["Data"]["Files"] if f["Type"] == "blob" and (include is None or include(f["Path"]))]
    print(f"Preparing {repo}: {sum(f['Size'] for f in files)/1024**3:.2f} GiB", flush=True)
    def fetch(entry):
        path = (destination / entry["Path"]).resolve()
        if not path.is_relative_to(destination.resolve()):
            raise ValueError("Unsafe model path")
        if entry["Size"] == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return
        url = f"https://modelscope.cn/models/{repo}/resolve/master/{quote(entry['Path'], safe='/')}"
        download(url, path, entry.get("Sha256") or None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, files))
    state[repo] = {"source": "modelscope", "files": [{"path": f["Path"], "sha256": f.get("Sha256"), "size": f["Size"]} for f in files]}


def install_models(endpoint, state):
    if endpoint == "modelscope":
        ms_snapshot("pengzhendong/faster-whisper-large-v3", ROOT / "models" / "faster-whisper-large-v3", state, lambda name: name.endswith((".bin", ".json", ".txt")))
        ms_snapshot("IndexTeam/IndexTTS-2.5", ROOT / "models" / "IndexTTS-2.5", state, lambda name: not name.startswith("qwen0.6bemo4-merge/"))
        ms_snapshot("Qwen/Qwen3-8B-GGUF", ROOT / "models", state, lambda name: name == "Qwen3-8B-Q4_K_M.gguf")
        install_auxiliary(state)
        state["model_endpoint"] = endpoint
        return
    os.environ["HF_ENDPOINT"] = endpoint
    os.environ["HF_HOME"] = str(ROOT / "models" / "hf-cache")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    api = HfApi(endpoint=endpoint)

    for repo, destination, patterns in [
        ("Systran/faster-whisper-large-v3", ROOT / "models" / "faster-whisper-large-v3", ["*.bin", "*.json", "*.txt"]),
        ("IndexTeam/IndexTTS-2.5", ROOT / "models" / "IndexTTS-2.5", None),
    ]:
        print("Preparing model: " + repo, flush=True)
        revision = api.model_info(repo).sha
        snapshot_download(repo, revision=revision, local_dir=destination, allow_patterns=patterns, max_workers=3, endpoint=endpoint)
        state[repo] = revision

    repo = "Qwen/Qwen3-8B-GGUF"
    info = api.model_info(repo)
    candidates = [s.rfilename for s in info.siblings if s.rfilename.lower().endswith("q4_k_m.gguf")]
    if len(candidates) != 1:
        raise RuntimeError("Cannot identify the single-file official Qwen3-8B Q4_K_M model")
    print("Preparing local translator: " + candidates[0], flush=True)
    source = Path(hf_hub_download(repo, candidates[0], revision=info.sha, local_dir=ROOT / "models" / "qwen-download", endpoint=endpoint))
    destination = ROOT / "models" / "Qwen3-8B-Q4_K_M.gguf"
    if not destination.exists():
        try:
            os.link(source, destination)
        except OSError:
            shutil.copyfile(source, destination)
    state[repo] = info.sha
    auxiliary = ROOT / "models" / "IndexTTS-2.5" / "hf_cache"
    snapshot_download("facebook/w2v-bert-2.0", local_dir=auxiliary / "w2v-bert-2.0", allow_patterns=["config.json", "preprocessor_config.json", "model.safetensors"], endpoint=endpoint)
    hf_hub_download("funasr/campplus", "campplus_cn_common.bin", local_dir=auxiliary, endpoint=endpoint)
    for name in ["config.json", "bigvgan_generator.pt"]:
        hf_hub_download("nvidia/bigvgan_v2_22khz_80band_256x", name, revision="fede455c2532b4f611fcef720825137b405af776", local_dir=auxiliary / "bigvgan", endpoint=endpoint)
    state["model_endpoint"] = endpoint


def install_auxiliary(state):
    auxiliary = ROOT / "models" / "IndexTTS-2.5" / "hf_cache"
    ms_snapshot("AI-ModelScope/w2v-bert-2.0", auxiliary / "w2v-bert-2.0", state, lambda name: name in {"config.json", "preprocessor_config.json", "model.safetensors"})
    ms_snapshot("iic/speech_campplus_sv_zh-cn_16k-common", auxiliary, state, lambda name: name == "campplus_cn_common.bin")
    for name in ("config.json", "bigvgan_generator.pt"):
        path = auxiliary / "bigvgan" / name
        revision = "fede455c2532b4f611fcef720825137b405af776"
        checksum = "ee7862b70ad758a26e61f9901f3d3cbd187c26ea52d8adf8bded12bfe0603edb" if name.endswith(".pt") else None
        download(f"https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x/resolve/{revision}/{name}", path, checksum)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="modelscope")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("UV_CACHE_DIR", str(ROOT / "runtime" / "uv-cache"))
    os.environ.setdefault("UV_PYTHON_INSTALL_DIR", str(ROOT / "runtime" / "python"))
    state_path = ROOT / "runtime" / "installation.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    try:
        if not args.models_only:
            install_source(state)
            atomic_json(state_path, state)
            if not args.source_only:
                install_llama(state)
                atomic_json(state_path, state)
        if not args.runtime_only and not args.source_only:
            install_models(args.endpoint, state)
            atomic_json(state_path, state)
            subprocess.run([sys.executable, ROOT / "scripts" / "prepare_background.py"], check=True)
        print("Preparation complete. Video processing itself is offline.", flush=True)
    finally:
        atomic_json(state_path, state)
