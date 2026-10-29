"""Real local serial/batch comparison; outputs never touch project voice caches."""
import json
import os
from pathlib import Path
import sys
import wave
from model_worker import block_network
from filelock import FileLock


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(request["root"])
    os.chdir(request["index_root"]); sys.path.insert(0, request["index_root"])
    block_network()
    import torch
    from indextts.infer_v2_5 import IndexTTS2
    from tts_batch import run_clips
    model = IndexTTS2(cfg_path=str(Path(request["model"]) / "config.yaml"), model_dir=request["model"], use_bf16=True, use_cuda_kernel=False, use_deepspeed=False)
    warm = request["clips"][0]
    model.infer(spk_audio_prompt=warm["reference"], text=warm["text"], output_path=str(root / "tts-warm.wav"), lang="ZH", verbose=False)
    results = {}
    for label, size in [("serial", 1), ("batch2", 2)]:
        target = root / ("tts-" + label); target.mkdir(exist_ok=True)
        clips = [dict(c, output=str(target / f"{i}.wav")) for i,c in enumerate(request["clips"])]
        for c in clips: Path(c["output"]).unlink(missing_ok=True)
        torch.manual_seed(42); torch.cuda.reset_peak_memory_stats()
        stats = run_clips(model, clips, size)
        audio = 0
        for c in clips:
            with wave.open(c["output"], "rb") as wav: audio += wav.getnframes() / wav.getframerate()
        stats["audio_seconds"] = audio; stats["rtf"] = stats["seconds"] / audio
        results[label] = stats
        (root / "tts-benchmark.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    results["speedup"] = results["serial"]["seconds"] / results["batch2"]["seconds"]
    (root / "tts-benchmark.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    with FileLock(Path(__file__).resolve().parents[1] / "runtime/gpu-model.lock", timeout=3600): main()
