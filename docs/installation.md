# 安装与排错

## 准备条件

- Windows 10/11 x64；完整安装脚本面向 Windows，其他系统尚未提供完整支持。
- Git 与 uv。建议使用已验证的 uv 0.11.16；应用环境使用 Python 3.12，IndexTTS 环境使用 Python 3.11，由 uv 管理。
- FFmpeg 和 FFprobe 均加入 PATH。Windows 可使用 `winget install --id Gyan.FFmpeg --exact`；安装后重新打开 PowerShell，执行 `ffmpeg -version`、`ffprobe -version` 确认。
- NVIDIA 显卡与兼容 CUDA 12.8 的驱动。当前 IndexTTS 配音要求 CUDA，不提供 CPU 配音路径。翻译使用 Vulkan llama.cpp，需要显卡驱动支持 Vulkan。
- 当前默认模型在 16GB 显存 / 32GB 内存机器验证过；不足时可以减小配音批量，但不保证所有硬件均能加载。下载、三个环境、缓存与视频副本会占用大量空间。

## 安装

```powershell
git clone https://github.com/MrmoLabs/BTranslateVideo.git
cd BTranslateVideo
.\setup.ps1
```

如果 PowerShell 执行策略阻止脚本，可对本次进程使用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1`。不需要永久修改系统执行策略。脚本会检查 FFmpeg/FFprobe，创建应用环境、下载固定版本的 IndexTTS 源码、建立独立配音/背景分离环境并准备权重。首次安装耗时取决于网络、磁盘和模型文件大小。

默认模型通过 ModelScope 下载；llama.cpp 来自官方 GitHub Release，部分辅助模型来自 Hugging Face。下载源不同不代表全程不访问其他站点。准备完成后，识别、配音、分离进程阻止联网，翻译只接受本机服务地址。

```powershell
# 改用 Hugging Face
.\setup.ps1 -ModelEndpoint https://huggingface.co

# 仅为模型/运行时下载使用已有代理，不写入仓库或全局 Git 配置
.\setup.ps1 -DownloadProxy 'socks5://127.0.0.1:你的端口'

# 只准备运行时，暂不下载模型权重
.\setup.ps1 -SkipModels
```

`-SkipModels` 仍准备 IndexTTS 环境和 llama.cpp，因此仍可能下载依赖；它不是离线安装选项。下载失败可重新执行以续传。不要在应用正在处理视频时运行环境准备或更新命令。

## 启动、停止与更新

双击 `start.bat`，打开 http://127.0.0.1:7861。双击 `stop.bat` 取消任务并停止服务，已经完成的片段保留。设置默认值随代码提供；页面修改后写入被 Git 忽略的 `settings.local.json`。示例见根目录 `settings.example.json`，不需要复制才能启动。

更新前停止服务并备份重要 `projects/` 和 `settings.local.json`，再执行 `git pull --ff-only` 与 `uv sync --locked --group dev --extra cuda`。涉及模型或运行时的变化应阅读 CHANGELOG，再决定重跑 setup。更新不会自动升级已有成片；需要选择项目重新配音或导出。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| uv / FFmpeg 未找到 | 重开终端并确认命令在 PATH；不要只安装 FFmpeg Python 包 |
| 下载失败 | 查看失败站点，重试或配置 DownloadProxy；代理参数不保证适用于 uv 的包下载，uv 可使用其官方代理环境变量 |
| 显存/内存不足 | 关闭占用较大的应用，配音批量改为 1 或 2，避免其他模型同时常驻 |
| GPU 配音加载失败 | 在诊断日志检查驱动、CUDA 与独立 IndexTTS 环境；不要混用应用环境里的 Torch |
| 中文配音长于窗口 | 缩短冗余译文或调整时间；程序会在上限内加速，不截断内容 |
| 原视频不能播放 | HEVC 等编码可能不受浏览器支持；最终页面预览会转为 H.264/AAC |
| 关闭字幕仍看到字 | 开关只控制后加中文字幕；原画面内的字幕无法通过该开关隐藏 |
| 背景仍有人声 | 分离模型的限制；可上传独立背景音，或者选择仅中文配音 |

反馈时附系统、显卡、阶段和去除私人信息的错误片段；不要上传模型、完整日志、参考音或私有视频。
