"""Prepare the optional isolated Fun-ASR runtime and official ModelScope weights."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.file_io import atomic_json

FUNASR_VERSION = '1.4.26'
MODELS = [('FunAudioLLM/Fun-ASR-Nano-2512', 'models/Fun-ASR-Nano-2512'),
          ('iic/speech_fsmn_vad_zh-cn-16k-common-pytorch', 'models/fsmn-vad')]


def reuse_torch(source, destination):
    """Copy compatible installed wheels; never modify the source environment."""
    source_python = source / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    info = json.loads(subprocess.check_output([str(source_python), '-c',
        'import sys,json,importlib.metadata as m; print(json.dumps(dict(python=list(sys.version_info[:2]),'
        'torch=m.version("torch"),torchaudio=m.version("torchaudio"))))'], text=True))
    if info['python'] != [3, 11] or info['torch'] != info['torchaudio']:
        raise ValueError('Reuse requires matching torch/torchaudio wheels in a Python 3.11 environment')
    site = source / ('Lib/site-packages' if os.name == 'nt' else 'lib/python3.11/site-packages')
    target = destination / ('Lib/site-packages' if os.name == 'nt' else 'lib/python3.11/site-packages')
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        if distribution.metadata['Name'].lower() not in {'torch', 'torchaudio'}:
            continue
        roots = {Path(str(item)).parts[0] for item in distribution.files if '..' not in Path(str(item)).parts}
        for name in roots:
            path = site / name
            if path.is_dir():
                shutil.copytree(path, target / name, dirs_exist_ok=True)
            elif path.is_file():
                shutil.copy2(path, target / name)
    return info['torch']


def prepare_runtime(device, reuse=None):
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('Install uv first: https://docs.astral.sh/uv/')
    folder = ROOT / 'runtime/fun-asr-env'
    python = folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    env = {**os.environ, 'UV_CACHE_DIR': str(ROOT / 'runtime/uv-cache'), 'PIP_CACHE_DIR': str(ROOT / 'runtime/pip-cache'), 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    def run(*args):
        subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True)
    if not python.is_file():
        run(uv, 'venv', folder, '--python', '3.11', '--seed')
    torch_version = reuse_torch(reuse.resolve(), folder) if reuse else '2.9.0'
    run(python, '-m', 'pip', 'install', f'torch=={torch_version}', f'torchaudio=={torch_version}', '--index-url',
        'https://download.pytorch.org/whl/cu128' if device == 'cuda' else 'https://download.pytorch.org/whl/cpu')
    run(python, '-m', 'pip', 'install', f'funasr=={FUNASR_VERSION}', 'transformers==4.57.6',
        'tiktoken', 'filelock', 'soundfile', '--index-url', 'https://pypi.org/simple')
    run(python, '-m', 'pip', 'check')
    run(python, '-c', 'import torch; from funasr.models.fun_asr_nano.model import FunASRNano; '
        + ('assert torch.cuda.is_available(), "CUDA unavailable"; ' if device == 'cuda' else '')
        + 'print("Fun-ASR runtime ready:", torch.__version__, "CUDA:", torch.cuda.is_available())')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(repository, destination):
    api = f'https://modelscope.cn/api/v1/models/{repository}/repo/files?Revision=master&Recursive=true'
    with urllib.request.urlopen(api, timeout=60) as response:
        listing = json.load(response)
    files = [item for item in listing['Data']['Files'] if item['Type'] == 'blob'
             and not item['Path'].startswith(('images/', 'fig/')) and item['Path'] != '.gitattributes']
    manifest = []
    for item in files:
        relative = Path(item['Path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid model file path')
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = item.get('Sha256') or item.get('SHA256')
        if not path.is_file() or path.stat().st_size != item['Size'] or expected and sha256(path) != expected:
            print(f"Downloading {repository}/{item['Path']} ({item['Size'] / 1024**2:.1f} MiB)", flush=True)
            url = f"https://modelscope.cn/models/{repository}/resolve/master/" + urllib.parse.quote(item['Path'], safe='/')
            temporary = path.with_name(path.name + '.part')
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as stream:
                shutil.copyfileobj(response, stream, length=8 * 1024 * 1024)
            if temporary.stat().st_size != item['Size'] or expected and sha256(temporary) != expected:
                raise ValueError(f'Download verification failed: {relative}')
            os.replace(temporary, path)
        manifest.append(dict(path=item['Path'], size=path.stat().st_size, sha256=sha256(path)))
    atomic_json(destination / 'btv-manifest.json', dict(repository=repository, revision='master', files=manifest))
    print('Model files ready:', destination, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=['cuda', 'cpu'], default='cuda')
    parser.add_argument('--runtime-only', action='store_true')
    parser.add_argument('--skip-runtime', action='store_true')
    parser.add_argument('--reuse-torch-from', type=Path, help='Copy matching Python 3.11 torch/torchaudio wheels from an existing environment')
    args = parser.parse_args()
    if args.runtime_only and args.skip_runtime:
        parser.error('--runtime-only and --skip-runtime cannot be combined')
    if not args.skip_runtime:
        prepare_runtime(args.device, args.reuse_torch_from)
    if not args.runtime_only:
        for repository, relative in MODELS:
            download_model(repository, ROOT / relative)
    print('Select Fun-ASR-Nano in Settings > Models and tools > Transcription. For Sichuan/Chongqing choose Chinese.', flush=True)


if __name__ == '__main__':
    main()
