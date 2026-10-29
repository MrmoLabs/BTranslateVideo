# 参与贡献 / Contributing

欢迎提交安装体验、翻译质量、配音调度和界面改进。先查阅现有 Issues；较大的变更先说明场景、实现方向及验收方式。

## 本地开发

```powershell
git clone https://github.com/你的账号/BTranslateVideo.git
cd BTranslateVideo
uv sync --locked --group dev
uv run --no-sync pytest -q --basetemp runtime/pytest-temp
node --check app/static/app.js
python scripts/check_repository.py
```

安装 Git、uv、Node.js（仅用于 JavaScript 语法检查）和 FFmpeg/FFprobe。单元与媒体回归无需模型和 GPU。完整推理见安装指南。没有前端打包步骤，静态文件直接由 FastAPI 提供。

## 提交方式

Fork → 新建功能分支 → 提交变更 → 向本仓库 `main` 提交 PR。一次 PR 聚焦一个问题；说明触发场景、行为变化、验证结果和未验证范围。不要把私人项目、参考音、模型、输出或本地设置加入提交。

所有新建/重写的机器配置文件均使用 **UTF-8 无 BOM**。Windows PowerShell 5.1 不要依赖 `Set-Content -Encoding utf8` 或 `Out-File -Encoding utf8` 生成无 BOM 文件；可用 Python `write_text(encoding='utf-8')` 或 .NET `UTF8Encoding(false)`。

修改推理后端时确认情绪、音色、缓存与时间对齐兼容；修改编辑 UI 时验证未保存草稿和迟到响应保护。测试用合成数据和可再分发素材，不加入未经授权的第三方内容。

原创代码贡献按 MIT 授权；带独立许可证的文件继续遵守其文件头及 NOTICE，尤其是 IndexTTS 适配文件。不要移除原作者归属或把第三方代码统一改为 MIT。

Please describe reproducible behavior, changes and verification in each PR. Model tests use doubles; do not represent them as real inference or listening validation. Keep private media and credentials out of commits, and preserve third-party licensing notices.
