"""Inspect staged/tracked source before publication; never reads ignored data."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {"data", "projects", "models", "runtime", "vendor", ".venv", ".git"}
MEDIA = {".wav", ".flac", ".mp3", ".mp4", ".mkv", ".avi", ".mov", ".webm",
         ".gguf", ".pth", ".pt", ".safetensors", ".onnx", ".bin", ".zip", ".7z"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
PERSONAL_PATH = re.compile(r"[A-Za-z]:\\(?:Users|Projects)\\", re.I)


def audit_files(root: Path, names: list[str]) -> list[str]:
    errors = []
    for name in names:
        relative = Path(name)
        path = root / relative
        lower = relative.name.lower()
        if (not relative.parts or relative.is_absolute() or ".." in relative.parts
                or any(p.lower() in PRIVATE_DIRS for p in relative.parts)
                or lower in {"settings.local.json", "acceptance.md"}
                or lower == ".env" or lower.startswith(".env.") and lower != ".env.example"
                or relative.suffix.lower() in MEDIA | {".pem", ".key", ".pfx", ".p12", ".log"}):
            errors.append(f"{name}: local/private artifact is not publishable")
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"{name}: missing file or symlink")
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            errors.append(f"{name}: exceeds source-file size budget (2 MiB)")
            continue
        data = path.read_bytes()
        if relative.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico"}:
            if relative.parts[:2] != ("docs", "assets"):
                errors.append(f"{name}: image must be an intentional docs asset")
            continue
        if data.startswith(b"\xef\xbb\xbf"):
            errors.append(f"{name}: UTF-8 BOM is not allowed")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{name}: unexpected binary or non-UTF-8 text")
            continue
        if SECRET.search(text):
            errors.append(f"{name}: possible credential; value omitted")
        if PERSONAL_PATH.search(text):
            errors.append(f"{name}: machine-specific personal path")
        try:
            if relative.suffix == ".json":
                json.loads(text)
            elif relative.suffix == ".toml":
                tomllib.loads(text)
        except ValueError:
            errors.append(f"{name}: malformed machine configuration")
        if relative.suffix == ".md":
            for link in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
                if ":" in link or link.startswith("#"):
                    continue
                target = link.split("#", 1)[0].strip("<>")
                if target and not (path.parent / target).exists():
                    errors.append(f"{name}: broken local link {target}")
    return errors


def main():
    result = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=ROOT,
                            capture_output=True, check=True)
    names = result.stdout.decode("utf-8").strip("\0").split("\0")
    if not names or names == [""]:
        raise SystemExit("No tracked/staged source files found; stage an explicit source list first.")
    errors = audit_files(ROOT, names)
    required = {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES/IndexTTS.txt",
                "README.md", "README.en.md", "settings.example.json", ".github/workflows/ci.yml"}
    errors += [f"Missing required source file: {p}" for p in sorted(required - set(names))]
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print(f"Publication audit passed: {len(names)} source files; no ignored local artifacts inspected.")


if __name__ == "__main__":
    main()
