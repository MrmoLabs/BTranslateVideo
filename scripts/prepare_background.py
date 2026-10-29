"""Prepare the isolated offline background separator without changing TTS deps."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import atomic_json
from scripts.prepare_models import download, digest


def main():
    python = ROOT / "runtime" / "separation-env" / "Scripts" / "python.exe"
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found; install uv before preparing background separation")
    index_python = ROOT / "vendor/index-tts/.venv/Scripts/python.exe"
    if not index_python.is_file():
        raise RuntimeError("IndexTTS environment is missing; run setup.ps1 first")
    if not python.exists():
        subprocess.run([uv, "venv", str(python.parents[1]), "--python", str(index_python)], check=True)
    site = python.parents[1] / "Lib/site-packages"
    (site / "btranslate-torch.pth").write_text(str(ROOT / "vendor/index-tts/.venv/Lib/site-packages") + "\n", encoding="utf-8")
    packages = ["demucs==4.0.1", "dora-search==0.1.12", "treetable==0.2.5", "openunmix==1.3.0", "lameenc==1.8.1", "submitit==1.5.3", "retrying==1.4.2", "omegaconf==2.3.0", "cloudpickle==3.1.1"]
    subprocess.run([uv, "pip", "install", "--python", str(python), "--no-deps", "--index-url", "https://pypi.org/simple", *packages], check=True)
    model = ROOT / "models/demucs"; model.mkdir(parents=True, exist_ok=True)
    url = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"
    expected = "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"
    download(url, model / "955717e8-8726e21a.th", expected)
    if not all((model / name).is_file() for name in ["htdemucs.safetensors", "config.json", "manifest.json"]):
        atomic_json(model / "manifest.json", dict(name="htdemucs", url=url, sha256=expected, license="MIT", package="demucs==4.0.1"))
        subprocess.run([python, ROOT / "scripts/convert_demucs.py"], check=True)
    print("Background separation ready; inference runs fully offline.")


if __name__ == "__main__": main()
