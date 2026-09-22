# Build the two release packages.
#
#   F1Engineer-core-<ver>-win64.zip   light PyInstaller onedir exe (~30MB)
#   F1Engineer-full-<ver>-win64.zip   source + stt_lib + stt_models + embedded
#                                     Python + 启动.bat  (~760MB, 解压即用)
#
# Usage:
#   pwsh -File build_release.ps1 -Version 0.2.0
#   pwsh -File build_release.ps1 -Version 0.2.0 -CoreOnly
#   pwsh -File build_release.ps1 -Version 0.2.0 -FullOnly
#
# Requirements: PyInstaller (installed into build_lib/ if missing), and an
# embedded Python zip from python.org for the full package.

param(
    [string]$Version = "0.2.0",
    [switch]$CoreOnly,
    [switch]$FullOnly,
    [string]$SttLib  = "stt_lib",
    [string]$SttModels = "stt_models",
    [string]$BuildLib = "build_lib"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root
$dist = Join-Path $root "dist"
$work = Join-Path $root "build"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

function Resolve-UnderRoot($p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return $p }
    return Join-Path $root $p
}

function Assert-Path($p, $what) {
    if (-not (Test-Path $p)) { throw "缺少 ${what}: $p" }
}

# ---------------------------------------------------------------- core (exe)
function Build-Core {
    Write-Host "== 构建轻量核心包 (PyInstaller onedir) ==" -ForegroundColor Cyan
    $env:PYTHONPATH = Join-Path $root $BuildLib
    try { py -3.12 -c "import PyInstaller" 2>$null } catch {
        Write-Host "安装 PyInstaller 到 $BuildLib ..."
        py -3.12 -m pip install pyinstaller --target $BuildLib --quiet
    }
    py -3.12 -m PyInstaller `
        --noconfirm --clean --onedir --noupx `
        --name F1Engineer `
        --version-file version_info.txt `
        --paths . `
        --exclude-module tkinter `
        --exclude-module matplotlib `
        --exclude-module faster_whisper `
        --exclude-module ctranslate2 `
        --exclude-module sounddevice `
        --exclude-module numpy `
        --distpath $dist --workpath $work `
        FI.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

    $exeDir = Join-Path $dist "F1Engineer"
    # A default .env so first run has the right shape (never a real key).
    Copy-Item (Join-Path $root ".env.example") (Join-Path $exeDir ".env.example") -Force
    $zip = Join-Path $dist "F1Engineer-core-$Version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $exeDir -DestinationPath $zip
    Write-Host "-> $zip" -ForegroundColor Green
    Get-FileHash $zip -Algorithm SHA256 | ForEach-Object { "$($_.Hash)  $(Split-Path $zip -Leaf)" } |
        Add-Content (Join-Path $dist "SHA256SUMS.txt")
}

# ------------------------------------------------------------- full (embed)
function Build-Full {
    Write-Host "== 构建全量语音包 (embedded Python + 解压即用) ==" -ForegroundColor Cyan
    $SttLib = Resolve-UnderRoot $SttLib
    $SttModels = Resolve-UnderRoot $SttModels
    Assert-Path $SttLib "本地语音依赖目录 (faster-whisper)"
    Assert-Path $SttModels "语音模型目录"

    $stage = Join-Path $work "full"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null

    # 1) application source.
    #    - namespace packages (lib) are copied AS A DIRECTORY so `from lib.x`
    #      keeps working;
    #    - loose *.py files are copied individually.
    $skipNames = @("build_lib", "dist", "build", "sessions", "__pycache__",
                   "stt_lib", "stt_models", "python-embed", "png_src", ".git")

    # 1a) lib/ (must stay a package directory named "lib")
    Copy-Item (Join-Path $root "lib") (Join-Path $stage "lib") -Recurse -Force
    Get-ChildItem (Join-Path $stage "lib") -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # 1b) top-level .py files (skip tests/dev tools)
    $skipPy = @("tests_", "test_", "bench_", "capture", "probe_gamepad",
                "fake_data", "voice_runner", "tool_stt_mic", "download_stt_model")
    Get-ChildItem -Path $root -Filter "*.py" -File | ForEach-Object {
        $n = $_.Name
        if ($skipPy | Where-Object { $n.StartsWith($_) }) { return }
        Copy-Item $_.FullName (Join-Path $stage $n) -Force
    }

    # 1c) static files
    foreach ($f in @(".env.example", "README.md", "LICENSE", "启动.bat", "FIRST_RUN.txt")) {
        $src = Join-Path $root $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $stage $f) -Force }
    }

    # 2) local voice dependencies + model (the whole point of the full pack)
    Write-Host "  复制 $SttLib ..."
    Copy-Item $SttLib (Join-Path $stage "stt_lib") -Recurse -Force
    Write-Host "  复制 $SttModels ..."
    Copy-Item $SttModels (Join-Path $stage "stt_models") -Recurse -Force

    # 3) embedded Python runtime (must be provided; see FIRST_RUN.txt)
    $embed = Join-Path $root "python-embed"
    if (Test-Path $embed) {
        Copy-Item (Join-Path $embed "*") $stage -Recurse -Force
    } else {
        Write-Warning "未找到 python-embed (全量包将不附带 embedded Python)。请下载 python-3.12.x-embed-amd64.zip 解压到 python-embed/ 后重试。"
    }

    # 4) launcher bat (points at embedded python, falls back to py -3.12)
    Copy-Item (Join-Path $root "启动.bat") (Join-Path $stage "启动.bat") -Force

    # 5) zip (zip64 for >4GB safety)
    $zip = Join-Path $dist "F1Engineer-full-$Version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "  压缩中（约 700MB，可能需几分钟）..."
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
    Write-Host "-> $zip" -ForegroundColor Green
    Get-FileHash $zip -Algorithm SHA256 | ForEach-Object { "$($_.Hash)  $(Split-Path $zip -Leaf)" } |
        Add-Content (Join-Path $dist "SHA256SUMS.txt")
}

if (-not $FullOnly) { Build-Core }
if (-not $CoreOnly) { Build-Full }

Write-Host ""
Write-Host "完成。产物在 dist/ 目录" -ForegroundColor Green
Get-ChildItem $dist | Select-Object Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
