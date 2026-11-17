# Configurable local model backends / 可配置本地模型接口

Open **Settings → Models and tools** (设置 → 模型与工具). Each stage has its own backend, model path or local base URL, model ID and cache revision. Switching backends retains the other fields. Saving affects subsequently queued tasks; running tasks retain their settings snapshot. Existing settings continue to select faster-whisper, automatic translation routing and IndexTTS.

界面中的“检查连接与文件”使用当前草稿，不会保存设置、加载模型、发送视频或参考音。它检查本地文件或读取服务的 `GET {base_url}/models`。服务可达或模型已列出，不等于推理、语言、音质已经验证。模型 ID 不在列表时，可从模型输入框的候选项选择；不提供列表的服务仍可手动填写 ID。

## Supported adapters

| Stage | Backend ID | Configuration and contract |
| --- | --- | --- |
| Transcription | `faster_whisper` | Local CTranslate2 model directory, CPU/CUDA, compute type; no automatic downloads. |
| Transcription | `fun_asr` | Local Fun-ASR-Nano-2512 + FSMN-VAD; Chinese (including Sichuan/Chongqing accents), English, Japanese. Isolated Python runtime, real CTC character timestamps. |
| Transcription | `openai` | `asr_url`, `asr_name`; OpenAI-compatible multipart transcription with complete word timestamps. |
| Translation / segmentation | `auto` | Preserve the legacy rule: use `translation_url` if supplied, otherwise manage local llama.cpp. |
| Translation / segmentation | `llama_cpp` | Manage local `llama-server` and `translation_model` GGUF, even if a retained service URL exists. |
| Translation / segmentation | `openai` | `translation_url`, `translation_name`; local `/chat/completions`. Disable JSON response mode if the server rejects `response_format`; prompts and returned JSON are still validated. |
| Speech | `indextts` | Existing IndexTTS 2 / 2.5 environment, local model path, cloning, emotions, batching and warm residency. |
| Speech | `openai_speech` | `tts_url`, `tts_name`, `tts_voice`; `/audio/speech` returning WAV. One configured service voice for all speakers. No reference upload, cloning or emotion controls. |
| Speech | `btv_reference` | A local bridge implementing the BTV v1 contract below. Sends reference audio and explicit emotion to the bridge. Requires a server adapter; arbitrary TTS servers do not implement this automatically. |

服务地址是 **base URL**，不是完整方法地址。例如 `http://127.0.0.1:8000/v1`；程序追加 `/audio/speech`、`/audio/transcriptions`、`/chat/completions` 或 `/models`。桥接若部署在 `/v1`，程序向 `/v1/synthesize` 发请求。

Only loopback HTTP (`127.0.0.1`, `localhost`, `::1`) is accepted. URLs cannot contain credentials, query parameters or fragments. Requests ignore environment proxies and do not follow redirects. This release does not support authenticated service endpoints. Start your local model server separately: BTranslateVideo neither starts/stops it nor changes its loaded models. The server controls batching, residency and GPU allocation; built-in IndexTTS batch/idle settings apply only to IndexTTS. The request timeout setting applies to service transcription and speech.

本程序只向本机地址发送请求；外部本地服务是否进一步联网取决于服务本身。完全本地使用时，应在服务中选择本地权重并关闭云端转发。不要将“本地服务地址”理解成对服务内部执行方式的验证。

For service-only operation, install the application dependencies (`uv sync --locked --group dev`) and provide FFmpeg/FFprobe, then run `uv run --no-sync python run.py`. Full `setup.ps1` model downloads are optional when all inference stages use existing local services; built-in background separation still needs its own files if selected.

## Manual loading and residency / 手动加载与保温

Settings → Models and tools → Dubbing model includes **Load model**, **Cancel loading**, **Unload model**, current model path and loading logs. The video dubbing page has the same controls. Loading uses saved model paths, reserves the existing processing queue, and waits for a worker acknowledgement after weights are actually loaded. It does not create dummy speech or modify projects. Repeated loads reuse an unchanged model. Cancel stops the owned worker, releases its GPU lock and permits a later retry. Model unloading is blocked while synthesis or queued processing is active.

- `tts_keep_loaded=false`, `tts_idle_minutes=0`: unload after a synthesis task. An explicitly preloaded model stays until the next synthesis finishes or you unload it manually.
- `tts_keep_loaded=false`, `tts_idle_minutes>0`: keep idle for the configured 0–1440 minutes (decimal values supported), then release. Each real task resets the idle countdown.
- `tts_keep_loaded=true`: no idle timer; retain the model until manual release, cancellation/failure, app shutdown or another GPU processing stage needs the memory.

“应用保温设置”仅保存这两个字段，其他模型配置草稿保留。空闲模型立即采用新策略；设置为 0 时会释放当前空闲模型。正在运行的任务保留其配置快照，新策略用于之后启动的任务。修改普通设置而没有改变保温字段，不会重置倒计时或卸载已预加载的模型。持续保温会占用显存；语音识别、翻译或背景分离仍可按原有机制让出显存。

These controls manage the app-owned IndexTTS process. Generic speech and reference bridge protocols do not define model lifecycle endpoints, so external servers remain responsible for their own load/unload and residency. The UI disables unsupported manual-load actions instead of claiming a remote model is loaded. Services can continue to share their own resident models independently.

Manual lifecycle endpoints are local only: `GET /api/tts-session`, `POST /api/tts-session/load`, `POST /api/tts-session/cancel-load`, `POST /api/tts-session/release`, `PUT /api/tts-session/retention` with `{"tts_idle_minutes":10,"tts_keep_loaded":false}`, and `GET /api/tts-session/logs`. `alive` reports a worker process, while `loaded` confirms a completed model operation; spawning a process alone does not report model readiness.

## Speech and cache behavior

`tts_languages` declares the target codes supported by **your selected model** (comma-separated subset of `zh,en,ja,es,ar`). This is a user declaration, not a detected capability. The app rejects other targets before generation. Ordinary speech requests contain no emotion parameter; explicit emotions are rejected before any requests rather than silently ignored. Set expressions to inherit or choose a backend supporting emotion.

普通语音接口不需要参考音。小说可以沿用服务音色，无需先创建音色库；已有音色选择会保留，但在该接口下不生效。切回支持参考音的模型后可继续使用。原有视频校对、小说分句、取消、完成片段复用、视频时间对齐、自然语速与导出共用同一条处理链。

Service speech outputs are validated as WAV, normalized to 24 kHz mono PCM, and atomically published only after completion. Cancellation/failure removes partial downloads while preserving completed clips. The queue sends one sentence at a time; this version does not assert server-side batching or GPU utilization. Reference-bridge inputs are limited to 10 MiB and each returned sentence to 100 MiB.

ASR, translation and speech have independent revision fields. Change the relevant revision after replacing a service model under the same model ID. Speech cache identity includes protocol, URL, model ID, voice ID, language declaration and revision, plus text/language/reference/emotion as applicable. Native model identities retain existing file-based behavior and cache compatibility.

## Transcription contract

### Fun-ASR-Nano / 川渝方言

首次准备（需要联网，下载约 2.1 GB 识别权重及独立 PyTorch 运行环境）：

```powershell
.venv/Scripts/python.exe scripts/prepare_fun_asr.py
```

CPU 环境可加 `--device cpu`。已有环境仅下载模型可加 `--skip-runtime`；仅安装环境可加 `--runtime-only`。安装不改变应用主环境、IndexTTS 环境或当前识别后端。

准备后重启应用，在 **设置 → 模型与工具 → 语音识别** 中选择 **Fun-ASR-Nano · 川渝方言** 并保存。新建/导入川渝视频时，原声语言选择 **中文**；四川、重庆没有单独的语言代码。热词填写人名、地名和专有名词，以逗号分隔；热词不是方言切换开关。旧项目已有字幕会继续复用，若要更换后端识别整段视频，请使用“重新断句”；仅修正少量句子可在字幕表格中选择“原声重新识别”并核对候选后采用。

默认模型目录为 `models/Fun-ASR-Nano-2512`、VAD 目录为 `models/fsmn-vad`；Python 留空使用 `runtime/fun-asr-env`。保留 faster-whisper 的原路径，方便随时切回。识别设备选 NVIDIA GPU 或 CPU；Whisper 的 int8 精度选项不用于 Fun-ASR。

程序使用 ModelScope Nano 完整 CTC 权重；某些 Hugging Face / MLT / Transformers 导出只有文本，不能替换此目录。分段使用本地 FSMN-VAD，每次解码最长 30 秒，使用原文的真实字符时间戳；缺少时间戳、漏字、乱序或越界会明确失败，不按字数伪造对齐。长段连续说话在 30 秒边界的识别效果仍需人工核对。批量原声校对共享一个模型，识别子进程退出后释放显存。模型、VAD、分词配置或热词变更会使对应识别缓存失效。

运行时禁止模型进程联网，不执行下载目录的远程 Python，不自动安装依赖。方言实际准确率需要用目标视频验证；地区口音支持不代表所有川渝土话都能准确转写。

官方资料：[Fun-ASR 能力与模型说明](https://github.com/QwenAudio/Fun-ASR)、[FunASR Nano 时间戳实现](https://github.com/modelscope/FunASR/tree/main/funasr/models/fun_asr_nano)。

### Local compatible service

`POST {asr_url}/audio/transcriptions`, multipart form fields:

- `file`: preprocessed 16 kHz mono PCM16 WAV
- `model`: configured model ID
- `response_format`: `verbose_json`
- `timestamp_granularities[]`: `word`
- `language`: source language if selected; omitted for automatic detection

The JSON response must contain real, complete, ordered word timestamps in seconds, within the audio duration:

```json
{"text":"Hello world.","words":[{"word":"Hello","start":0.1,"end":0.4},{"word":"world.","start":0.5,"end":1.0}]}
```

接口只返回文本、句级时间戳、缺词或越界时间戳时，会明确失败并保留原字幕。程序不会按字数伪造词级时间戳。模型返回内容还需通过现有完整原词与语义断句校验。

## Ordinary speech contract

`POST {tts_url}/audio/speech`, JSON request:

```json
{"model":"local-tts","input":"Hello world.","voice":"default","response_format":"wav"}
```

The response body must be WAV audio. Voice IDs and model language coverage depend on the server. The app does not infer that a server supports cloning or emotions because it exposes this endpoint.

## Reference bridge: BTV v1

This is an app-defined extension, not part of the OpenAI audio protocol. Implement it around your local engine if its native API differs.

`POST {tts_url}/synthesize`, JSON request:

```json
{
  "protocol":"btv-tts-v1",
  "model":"my-local-cloning-model",
  "input":"你好，世界。",
  "response_format":"wav",
  "language":"zh",
  "reference_audio":{"mime_type":"audio/wav","data_base64":"<base64 WAV bytes>"},
  "emotion":{"preset":"happy","strength":"medium"}
}
```

Return WAV bytes on success and an HTTP error on unsupported language, emotion or model. The bridge must honor reference audio and explicit emotion or reject the request. Presets are `inherit`, `happy`, `angry`, `sad`, `afraid`, `disgusted`, `melancholic`, `surprised`, `calm`; strength is `low`, `medium`, `high`. `inherit` means no explicit emotion override. Keep both directions local; no local file path is sent for the bridge to read.

## Adding another adapter

`app/model_backends.py` owns backend descriptors, capability checks, service requests and cache identities. `app/config.py` validates settings; the settings page retains inputs for inactive adapters. Video and audiobook generation share `Pipeline.synthesize`; do not create separate model processes for every page or change clip timing inside an adapter.

For an engine with a different API, implement the BTV bridge or add a backend ID, explicit capabilities, settings validation, request handler and cache identity here. Include contract tests for unsupported features, cancellation, failed response handling and cache separation in `tests/test_model_backends.py`. Merely adding a model name or pointing a native adapter at unrelated weights does not make that model compatible.

Protocol references: [speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create), [transcription](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create), [Ollama local chat compatibility](https://docs.ollama.com/api/openai-compatibility).

Validation for this integration uses mock HTTP transports and an isolated local fixture with synthetic WAV audio. Real additional model quality and GPU behavior must be validated against the particular server and installed model.
