<p align="center"><img src="docs/assets/banner.svg" alt="BTranslateVideo — 本地视频，中文理解" width="900"></p>

<p align="center"><a href="README.en.md">English</a> · <a href="docs/installation.md">安装指南</a> · <a href="docs/usage.md">使用指南</a> · <a href="CONTRIBUTING.md">参与贡献</a></p>

# BTranslateVideo

**把英语、日语视频，变成方便理解的中文视频。**

面向课程和动漫的本地视频译制工作台：识别原文、按上下文翻译、逐句校对与情绪试听，再生成保留背景声的中文配音视频。环境准备完成后，视频处理在本机运行。

当前支持目标：**Windows 10/11 x64 + NVIDIA CUDA GPU**。项目处于早期阶段；提供源码与安装脚本，尚无一键安装包。主要应用代码使用 MIT，IndexTTS 适配文件和模型另有许可证，见[许可范围](#许可范围)。

## 从视频到中文成片

| 1 · 视频导入 | 2 · 字幕校对 | 3 · 配音与情绪 | 4 · 成片与下载 |
| --- | --- | --- | --- |
| 本地文件、英语/日语、课程/动漫 | 按句搜索、编辑、比较优化建议 | 角色参考音、8 种情绪、3 档强度、单句试听 | 浏览器播放、字幕开关、MP4 / 双音轨 MKV |

- **本地处理**：Whisper 识别、Qwen 翻译、IndexTTS 配音、HTDemucs 背景分离；无云端推理依赖。
- **先校对再生成**：翻译结合相邻原文、角色和术语表；优化建议逐句选择采用，保留人工修改。
- **听一句再决定**：用当前草稿试听情绪，音色取自角色参考音；试听不覆盖字幕和成片。
- **完整表达**：短配音保持原速；过长配音在设定上限内加速，无法容纳时指出段落，不截掉句尾。
- **继续已有进度**：按内容复用语音和背景缓存，同条件语音自适应合批，取消后可重试。
- **观看与核对**：中文 MP4 可在网页播放、开关字幕；MKV 保留原音轨与原画质，便于对照。

<p align="center"><img src="docs/assets/workspace.png" alt="字幕校对工作台，使用项目原创演示字幕" width="900"></p>

截图使用仓库内的原创示例字幕，不包含用户视频、音色样本或模型生成结果。

## 快速开始

需要 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)、FFmpeg/FFprobe（加入 PATH）及支持 CUDA 的 NVIDIA 驱动。当前默认模型组合在 RTX 5070 Ti 16GB / 32GB 内存上验证过；这不是最低硬件要求。模型与环境占用较大，请预留充足磁盘空间。

```powershell
git clone https://github.com/MrmoLabs/BTranslateVideo.git
cd BTranslateVideo
ffmpeg -version
uv --version
.\setup.ps1
.\start.bat
```

打开 **http://127.0.0.1:7861**。首次准备需要联网下载模型和运行时；之后可离线处理。模型下载默认使用 ModelScope；其他下载源、代理、FFmpeg 安装和排错见[安装指南](docs/installation.md)。使用前请阅读[第三方许可](THIRD_PARTY_NOTICES.md)。

无需上传自己的视频到 GitHub。素材、参考录音、模型、缓存、成片与本地设置均保存在已忽略的本地目录中。

## 本地模型与运行方式

| 环节 | 默认组件 |
| --- | --- |
| 语音识别 | faster-whisper large-v3 · CUDA FP16 |
| 翻译与审校 | Qwen3-8B Q4_K_M · llama.cpp Vulkan |
| 中文配音 | IndexTTS-2.5 · CUDA BF16 · 自适应最多 4 段合批 |
| 背景分离 | HTDemucs · 分块处理 |
| 混音与视频 | FFmpeg / FFprobe · H.264/AAC 页面预览 |

模型按阶段加载和退出，避免三套模型同时常驻显存。长视频校对每页显示 20 句；高级设置和诊断日志按需展开。详细设计见[架构说明](docs/architecture.md)。

## 使用边界

识别和翻译仍需校对；角色需要人工对应，情绪目前手动选择。背景分离可能残留原对白或损失音效，有独立背景音时可直接上传。不修改口型，也不擦除原画面字幕；页面字幕开关只控制后加的中文字幕。

实际流程验收以英语单人教程为主；日语/动漫的完整视频、多角色及所有硬件组合尚未全面验证。请先处理短片段确认效果。输入视频和参考声音需具备相应使用权限。

## 开发与贡献

```powershell
uv sync --locked --group dev
uv run --no-sync pytest -q
python scripts/check_repository.py
```

自动测试使用模型替身，真实 FFmpeg 测试需要 FFmpeg/FFprobe；不会在 CI 下载模型或证明模型听感。[贡献说明](CONTRIBUTING.md) · [测试范围](docs/testing.md) · [路线图](docs/roadmap.md) · [问题反馈](https://github.com/MrmoLabs/BTranslateVideo/issues/new/choose)

## 许可范围

本项目**原创应用代码与文档采用 [MIT](LICENSE)**。`scripts/tts_batch.py` 改编自 IndexTTS 推理代码，保留 [bilibili Model Use License Agreement](LICENSES/IndexTTS.txt)，不在 MIT 授权范围内。IndexTTS 本身也不是 MIT 模型；其他模型和运行时使用各自许可证。

完整范围、上游来源和限制请查看 [NOTICE](NOTICE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。下载的依赖、权重以及用户素材不随本仓库分发。本项目与 bilibili / IndexTeam 无隶属或官方合作关系。
