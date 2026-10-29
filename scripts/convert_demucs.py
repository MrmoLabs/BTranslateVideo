"""One-time restricted conversion of the fixed official Demucs checkpoint."""
import hashlib
import json
from fractions import Fraction
from pathlib import Path

import numpy as np
import torch
from demucs.htdemucs import HTDemucs
from safetensors.torch import save_file

ROOT = Path(__file__).resolve().parents[1] / "models" / "demucs"
OFFICIAL_SHA = "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"


def plain(value):
    if isinstance(value, dict): return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [plain(v) for v in value]
    if isinstance(value, Fraction): return float(value)
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    raise TypeError(type(value))


def main():
    source = ROOT / "955717e8-8726e21a.th"
    if hashlib.sha256(source.read_bytes()).hexdigest() != OFFICIAL_SHA:
        raise ValueError("Official checkpoint checksum mismatch")
    scalar = np._core.multiarray.scalar if hasattr(np, "_core") else np.core.multiarray.scalar
    allowed = [HTDemucs, Fraction, (scalar, "numpy.core.multiarray.scalar"), np.dtype, np.dtypes.Float64DType, np.dtypes.Int64DType]
    with torch.serialization.safe_globals(allowed):
        package = torch.load(source, map_location="cpu", weights_only=True)
    if package["klass"] is not HTDemucs: raise ValueError("Unexpected model class")
    save_file({key: value.contiguous().clone() for key, value in package["state"].items()}, str(ROOT / "htdemucs.safetensors"))
    (ROOT / "config.json").write_text(json.dumps(dict(args=plain(package["args"]), kwargs=plain(package["kwargs"])), ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    manifest["weights_sha256"] = hashlib.sha256((ROOT / "htdemucs.safetensors").read_bytes()).hexdigest()
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Restricted conversion completed:", manifest["weights_sha256"])


if __name__ == "__main__": main()
