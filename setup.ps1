param(
    [switch]$SkipModels,
    [ValidateSet('modelscope','https://hf-mirror.com','https://huggingface.co')]
    [string]$ModelEndpoint = 'modelscope',
    [string]$DownloadProxy = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot 'runtime\uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot 'runtime\python'
$env:HF_HOME = Join-Path $PSScriptRoot 'models\hf-cache'
$env:HF_ENDPOINT = $ModelEndpoint
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:HF_HUB_DISABLE_XET = '1'
if ($DownloadProxy) { $env:BTV_DOWNLOAD_PROXY = $DownloadProxy }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Please install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
foreach ($toolName in @('ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $toolName -ErrorAction SilentlyContinue)) {
        throw "Missing $toolName. Install FFmpeg, add it to PATH and reopen PowerShell. See docs/installation.md."
    }
}
& uv sync --locked --python 3.12 --group dev --extra cuda
if ($LASTEXITCODE -ne 0) { throw 'Application dependency installation failed.' }
& '.\.venv\Scripts\python.exe' 'scripts\prepare_models.py' --endpoint $ModelEndpoint $(if ($SkipModels) { '--runtime-only' })
if ($LASTEXITCODE -ne 0) { throw 'Model/runtime preparation failed. Rerun to resume downloads.' }
Write-Host 'Ready. Run start.bat to open BTranslateVideo.' -ForegroundColor Green
