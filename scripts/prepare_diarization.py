"""Optional online preparation for local Community-1; inference stays offline."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings, atomic_json
from app.diarization import MODEL_FILES, python_path, check_environment


def command(arguments, env):
    subprocess.run([str(value) for value in arguments],check=True,cwd=ROOT,env=env)


def prepare_runtime(device):
    uv=shutil.which('uv')
    if not uv:raise RuntimeError('Install uv first: https://docs.astral.sh/uv/')
    settings=Settings();python=python_path(settings);env=dict(os.environ)
    env.update(UV_CACHE_DIR=str(ROOT/'runtime/uv-cache'),PIP_DISABLE_PIP_VERSION_CHECK='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    if not python.is_file():command([uv,'venv',python.parent.parent,'--python','3.11'],env)
    command([python,'-m','ensurepip'],env)
    # Install GPU wheels first. pip retains the installed CUDA build when
    # resolving pyannote; TorchCodec's Windows wheel comes from PyPI.
    command([python,'-m','pip','install','torch==2.8.0','torchaudio==2.8.0','--index-url',
             'https://download.pytorch.org/whl/cu128' if device=='cuda' else 'https://download.pytorch.org/whl/cpu'],env)
    command([python,'-m','pip','install','pyannote.audio==4.0.7','torchcodec==0.7.0','--index-url','https://pypi.org/simple'],env)
    command([python,'-m','pip','check'],env)
    info=check_environment(settings)
    if not info['ok']:raise RuntimeError(info['message'])
    atomic_json(python.parent.parent/'btv-diarization-runtime.json',dict(torch=info['torch'],pyannote=info['pyannote'],cuda_available=info['cuda_available']))
    print(json.dumps(info,ensure_ascii=False,indent=2))


def download_model(destination):
    from huggingface_hub import HfApi, get_token, snapshot_download
    token=get_token()
    if not token:
        raise RuntimeError('No local Hugging Face login. Accept Community-1 access conditions yourself, then run .venv/Scripts/hf.exe auth login. Never place a token in project settings or source files.')
    repository='pyannote/speaker-diarization-community-1'
    api=HfApi(endpoint='https://huggingface.co')
    info=api.model_info(repository,token=token)
    revision=info.sha
    snapshot_download(repository,revision=revision,local_dir=destination,
                      cache_dir=ROOT/'models/hf-cache/hub',token=token,endpoint='https://huggingface.co')
    files=[]
    for name in MODEL_FILES:
        path=destination/name
        if not path.is_file():raise RuntimeError('Incomplete Community-1 snapshot: '+name)
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            while data:=stream.read(4*1024*1024):digest.update(data)
        files.append(dict(path=name,size=path.stat().st_size,sha256=digest.hexdigest()))
    atomic_json(destination/'btv-manifest.json',dict(repository=repository,revision=revision,license='CC-BY-4.0',files=files))
    print('Community-1 snapshot ready at',destination)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-only',action='store_true',help='Install dependencies without downloading weights or using a Hugging Face login')
    parser.add_argument('--skip-runtime',action='store_true',help='Download weights only; keep the installed inference environment')
    parser.add_argument('--device',choices=['cuda','cpu'],default='cuda')
    parser.add_argument('--model-dir',type=Path,default=ROOT/'models/speaker-diarization-community-1')
    args=parser.parse_args()
    if args.runtime_only and args.skip_runtime:parser.error('runtime-only and skip-runtime cannot be combined')
    if not args.skip_runtime:prepare_runtime(args.device)
    if not args.runtime_only:download_model(args.model_dir.resolve())


if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Do not log tokens, request headers or authenticated URLs.
        if type(exc).__name__ in {'GatedRepoError','HfHubHTTPError','RepositoryNotFoundError'}:
            print('Official model access/download failed. Check your account permission and local login; credentials are omitted.',file=sys.stderr)
        else:print(str(exc),file=sys.stderr)
        sys.exit(1)
