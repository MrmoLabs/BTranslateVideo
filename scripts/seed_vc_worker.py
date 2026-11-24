"""Batch offline voice conversion with Seed-VC.

Runs inside vendor/seed-vc/.venv so its pinned PyTorch/transformers stack does
not affect the application environment. Reads one request JSON, loads the
22.05 kHz VC model once, and converts every clip in the batch.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from model_worker import write_progress


def main() -> int:
    request_path = Path(sys.argv[1]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    root = Path(request["root"]).resolve()
    if not (root / "seed_vc_wrapper.py").is_file():
        raise SystemExit(f"Seed-VC 源码不存在：{root}")

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    import torch
    import soundfile as sf
    from hf_utils import load_custom_model_from_hf
    from seed_vc_wrapper import SeedVCWrapper

    # Only the 22.05 kHz voice-conversion stack is needed here; the F0/SVC model
    # (~44.1 kHz) and RMVPE are for singing and would waste VRAM and load time.
    def load_minimal_modules(self):
        from modules.bigvgan import bigvgan
        from modules.campplus.DTDNN import CAMPPlus
        ckpt = load_custom_model_from_hf("funasr/campplus", "campplus_cn_common.bin", config_filename=None)
        self.campplus_model = CAMPPlus(feat_dim=80, embedding_size=192)
        self.campplus_model.load_state_dict(torch.load(ckpt, map_location="cpu"))
        self.campplus_model.eval().to(self.device)
        local_bigvgan = root / "local-models" / "bigvgan-22k"
        source = str(local_bigvgan) if (local_bigvgan / "config.json").is_file() else "nvidia/bigvgan_v2_22khz_80band_256x"
        self.bigvgan_model = bigvgan.BigVGAN.from_pretrained(source, use_cuda_kernel=False)
        self.bigvgan_model.remove_weight_norm()
        self.bigvgan_model = self.bigvgan_model.eval().to(self.device)
        self.bigvgan_44k_model = None
        self.rmvpe = None

    SeedVCWrapper._load_f0_model = lambda self: None  # noqa: SLF001
    SeedVCWrapper._load_additional_modules = load_minimal_modules  # noqa: SLF001

    device = torch.device("cuda" if request.get('device') != 'cpu' and torch.cuda.is_available() else "cpu")
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    print(f'Seed-VC device={device}', flush=True)
    return convert(request, request_path, device, SeedVCWrapper, torch, sf)


def convert(request, request_path, device, wrapper_class, torch, sf):
    from filelock import FileLock

    gpu = FileLock(Path(__file__).resolve().parents[1] / 'runtime' / 'gpu-model.lock', timeout=86400)
    with gpu if device.type == 'cuda' else nullcontext():
        return run_clips(request, request_path, wrapper_class(device=device), torch, sf)


def run_clips(request, request_path, wrapper, torch, sf):
    steps = int(request.get("steps", 25))
    cfg_rate = float(request.get("cfg_rate", 0.7))
    progress_path = Path(request.get('progress') or request_path.parent / 'vc-progress.json')
    result_path = request_path.parent / "vc-result.json"
    result = []
    for index, clip in enumerate(request.get("clips", []), 1):
        generated = wrapper.convert_voice(
            clip["source"],
            clip["target"],
            diffusion_steps=steps,
            length_adjust=1.0,
            inference_cfg_rate=cfg_rate,
            f0_condition=False,
            auto_f0_adjust=False,
            pitch_shift=0,
            stream_output=False,
        )
        # convert_voice contains yield statements, so it always returns a generator.
        if hasattr(generated, "__next__"):
            try:
                while True:
                    next(generated)
            except StopIteration as stop:
                generated = stop.value
        audio = generated
        if audio is None:
            raise RuntimeError(f"Seed-VC 没有返回音频：{clip['id']}")
        output = Path(clip["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        samples = torch.as_tensor(audio, dtype=torch.float32).detach().cpu().numpy().reshape(-1)
        pending = output.with_suffix('.pending.wav')
        sf.write(pending, samples, 22050, subtype='PCM_16')
        os.replace(pending, output)
        result.append(str(output))
        write_progress(progress_path, {"done": index, "total": len(request.get("clips", []))})
        print(f"VC {index}/{len(request.get('clips', []))} id={clip['id']}", flush=True)
    result_path.write_text(json.dumps({"outputs": result}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
