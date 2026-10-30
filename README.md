<p align="center"><img src="docs/assets/banner.svg" alt="BTranslateVideo — Local multilingual video dubbing" width="900"></p>

<p align="center"><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a> · <a href="docs/installation.md">Installation</a> · <a href="CONTRIBUTING.md">Contributing</a></p>

# BTranslateVideo

Translate and dub videos into Chinese, English, Japanese, Spanish or Arabic with a local, editable workflow. Transcribe, translate with context, review each sentence, audition emotional delivery, and export with background audio.

**Supported platform:** Windows 10/11 x64 with an NVIDIA CUDA GPU. Early-stage source project; no packaged installer yet. The UI supports Simplified Chinese and English; detailed guides are currently in Chinese.

## Workflow

1. **Import:** select a local video, its source and target languages, and its content type. Twelve explicit source languages plus auto-detection are available.
2. **Review:** search paginated sentences, edit translations and compare optional rewrite suggestions.
3. **Audition:** choose a reusable voice preset or project reference, then set emotion and intensity for individual or selected sentences. Audition unsaved drafts without replacing project outputs.
4. **Watch:** play dubbed MP4 previews with switchable subtitles; download an original-quality MKV with original and dubbed audio tracks.

Short utterances keep their natural speed. Longer utterances use bounded acceleration; sentences are never truncated to fit. Compatible voice conditions are batched, and unchanged audio is cached. HTDemucs estimates background audio; separation may leave dialogue or affect sound effects. Lip sync and removal of burned-in subtitles are not included.

<p align="center"><img src="docs/assets/workspace.png" alt="Sentence review workspace using authored example subtitles" width="900"></p>

The screenshot uses authored example subtitles; it includes no private videos, voice references or model outputs.

## Start

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), FFmpeg/FFprobe on PATH and an NVIDIA driver supporting the CUDA runtime. Read [third-party terms](THIRD_PARTY_NOTICES.md) before downloading models.

```powershell
git clone https://github.com/MrmoLabs/BTranslateVideo.git
cd BTranslateVideo
.\setup.ps1
.\start.bat
```

Open **http://127.0.0.1:7861**. Preparation downloads dependencies and weights; processing runs locally afterwards. ModelScope is the default model source. For Hugging Face use `.\setup.ps1 -ModelEndpoint https://huggingface.co`. See [installation](docs/installation.md) for troubleshooting and download proxy options.

The default stack is faster-whisper large-v3, Qwen3-8B Q4_K_M through llama.cpp Vulkan, IndexTTS-2.5 CUDA BF16, HTDemucs and FFmpeg. An RTX 5070 Ti 16GB / 32GB RAM machine has been tested; this is not a minimum requirement. Other GPUs and complete Japanese/anime/multi-speaker workflows need further validation.

## Languages and warm dubbing

Switch the UI language from the header without reloading or discarding drafts. The source selector supports English, Japanese, Korean, French, German, Spanish, Portuguese, Italian, Russian, Arabic, Hindi and Chinese, plus auto-detection. Translation and dubbing targets can be Chinese, English, Japanese, Spanish or Arabic with IndexTTS-2.5. Existing projects default to Chinese; IndexTTS-2 supports Chinese and English only. Source text, project names and raw diagnostic logs retain their original content. Full video quality still needs validation per language and content type.

IndexTTS stays warm for 10 idle minutes by default, so repeated auditions reuse its model and unchanged reference conditions. Set the timeout to 0–60 minutes or release GPU memory manually from the dubbing page. Other GPU model stages, cancellation, failures, model changes and application shutdown release the session. The worker uses a parent-owned local pipe and holds the GPU lock while warm; no extra network service is exposed.

## Voices, editing and diagnostics

The sidebar provides Projects, Audiobooks, Voice library and Settings pages. The project index supports import, search, rename, and recoverable Trash / restore. Upload, preview, edit or archive voice presets, then assign them to project speakers. Assignments keep independent reference copies, so later preset edits do not change existing projects.

The dubbing worksheet shows multiple sentences together, with text/speaker filters, 20/50/100 rows per page and selected-row emotion settings. Subtitle review uses directly editable source/translation rows. Both tables scroll with the page; sentence details and draft auditions open in place without moving the document. The Settings page retains drafts across menu changes and groups common controls separately from model/tool paths. The log panel supports search, error/warning filters, live following and full process-log downloads.

Change an existing project's target language on the video import page. The app backs up prior translations and outputs, keeps source text, timing and voice references, and asks you to translate and dub again. See the [usage guide](docs/usage.md) for details.

## Development

```powershell
uv sync --locked --group dev
uv run --no-sync pytest -q --basetemp runtime/pytest-temp
python scripts/check_repository.py
```

Tests use model doubles and real FFmpeg, not GPU inference. Input media, references, weights, caches, logs and local settings are excluded from Git. Read [CONTRIBUTING.md](CONTRIBUTING.md), [testing](docs/testing.md), [architecture](docs/architecture.md) and the [roadmap](docs/roadmap.md).

## License

Original application code and documentation are **[MIT](LICENSE)**. `scripts/tts_batch.py` adapts IndexTTS inference code and is instead covered by the **[bilibili Model Use License Agreement](LICENSES/IndexTTS.txt)**. Downloaded IndexTTS code/weights, other models and tools retain their own licenses. MIT does not relicense them. See [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Use videos and voice references you are entitled to use. This independent project is not affiliated with or endorsed by bilibili or IndexTeam.

### Recording and audiobooks

Create a voice from a file, a microphone recording, or Windows system audio (WASAPI loopback). Recording starts only on an explicit click, stops automatically at 29 seconds, and can be previewed before saving.

The Audiobooks menu organizes books into chapters. Paste prose, split it into editable sentences, choose a default voice or sentence overrides, and generate narration with progress and resumable clip caching. Chapters export as WAV / MP3; merge completed chapters into one book in chapter order. Narration retains its natural speed and shares the existing warm IndexTTS worker with video tasks. See the [usage guide](docs/usage.md). Local books and recordings are excluded from Git.
