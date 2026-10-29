# 第三方来源与许可 / Third-party notices

根目录 MIT 仅覆盖原创应用代码和文档，不覆盖以下代码、模型、运行时、用户输入或生成媒体。本清单说明直接集成的主要组件，不替代全部传递依赖的许可证。下载目录与 Python 包内的版权、LICENSE/NOTICE 应保留；再分发打包环境前需要另行核对实际构成。

## 仓库内的派生适配文件

`scripts/tts_batch.py` 改编自 IndexTTS `indextts/infer_v2_5.py`（版本 `d9e41aac89fd00b3d71497fddb287b7f24613712`），增加批处理、分组与回退。该文件保留 **bilibili Model Use License Agreement**，完整文本见 [LICENSES/IndexTTS.txt](LICENSES/IndexTTS.txt) 和 [上游固定版本](https://github.com/index-tts/index-tts/blob/d9e41aac89fd00b3d71497fddb287b7f24613712/LICENSE)。项目 MIT 不替代此授权。

IndexTTS 代码与权重有独立使用条件，并非 MIT，也不应将整套模型能力宣称为无条件商用。请直接阅读适用条款，尤其是大型服务使用、下游分发、模型改进和相关标识要求。具体使用是否符合条款由使用者自行核对。

按上游许可要求保留以下声明：

> Any modifications made to the original model in this Derivative Work are not endorsed, warranted, or guaranteed by the original right-holder of the original model, and the original right-holder disclaims all liability related to this Derivative Work.

## 下载的模型与工具（不随本仓库分发）

| 组件 | 上游 / 授权入口 |
| --- | --- |
| IndexTTS / IndexTTS-2.5 | [源码](https://github.com/index-tts/index-tts) · [权重与许可证](https://huggingface.co/IndexTeam/IndexTTS-2.5/blob/main/LICENSE) · bilibili Model Use License Agreement |
| Whisper / faster-whisper large-v3 | [Whisper](https://github.com/openai/whisper/blob/main/LICENSE) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) · MIT；转换权重见相应模型卡 |
| Qwen3-8B GGUF | [官方模型](https://huggingface.co/Qwen/Qwen3-8B-GGUF) · Apache-2.0，按所下载文件的 LICENSE/NOTICE 执行 |
| llama.cpp | [官方许可证](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) · MIT |
| HTDemucs / Demucs | [官方项目](https://github.com/facebookresearch/demucs) · MIT |
| w2v-BERT 2.0 | [官方模型卡](https://huggingface.co/facebook/w2v-bert-2.0) · MIT |
| CAM++ | [FunASR 模型卡](https://huggingface.co/funasr/campplus) · Apache-2.0；ModelScope 镜像也应核对对应条款 |
| BigVGAN | [官方项目与许可](https://github.com/NVIDIA/BigVGAN) · MIT；模型变体按其文件中的授权执行 |
| FFmpeg / FFprobe | [官方许可说明](https://ffmpeg.org/legal.html) · 依具体构建配置为 LGPL/GPL 等；本仓库不附带二进制 |
| PyTorch / CUDA Python 运行库 | [PyTorch](https://github.com/pytorch/pytorch/blob/main/LICENSE) 与 [NVIDIA CUDA 条款](https://docs.nvidia.com/cuda/eula/index.html)；不因本项目 MIT 改变 |

准备脚本记录已下载的来源、版本和校验信息到本地 `runtime/installation.json`。IndexTTS 源码固定版本；llama.cpp 使用安装时可用的官方 Vulkan Release，记录实际版本。不同时间准备的运行时可能有差异。

## 数据和项目归属

没有随项目再分发视频、真实音色样本、模型权重或生成音频。示例 SRT 是为本项目编写的合成文本。仓库标识与界面图形由本项目提供，不使用上游商标作为项目标识。本项目与 bilibili/IndexTeam 无隶属或背书关系。
