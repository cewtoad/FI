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
    # T10a/T10b: the whole argument list comes from the shared manifest so this
    # build and release.yml cannot drift (CI once lost --add-data data). Hidden
    # imports, excludes and the data/ resource are all handled there.
    # NOTE: do not name this $args - that is a PowerShell automatic variable.
    $pyiArgs = @(py -3.12 build_manifest.py pyinstaller-args)
    py -3.12 -m PyInstaller @pyiArgs --distpath $dist --workpath $work
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

    $exeDir = Join-Path $dist "F1Engineer"
    # A default .env so first run has the right shape (never a real key).
    Copy-Item (Join-Path $root ".env.example") (Join-Path $exeDir ".env.example") -Force
    $zip = Join-Path $dist "F1Engineer-core-$Version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    # T10a: zip via tools/make_zip.py (Compress-Archive OOMs on big trees);
    # arcnames are relative to the stage's parent -> a single F1Engineer/ folder.
    py -3.12 (Join-Path $root "tools\make_zip.py") $zip $exeDir
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

    # Stage named F1Engineer so the zip's top-level folder is F1Engineer/
    # (make_zip writes arcnames relative to the stage's parent).
    $stage = Join-Path $work "F1Engineer"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null

    # 1) application source + resources via the shared manifest whitelist
    #    (T10a: no more filename-prefix filtering drift; tests/ and tools/ are
    #    excluded by the manifest).
    Write-Host "  复制应用源码/资源（build_manifest 白名单）..."
    py -3.12 build_manifest.py full-copy --dest $stage
    if ($LASTEXITCODE -ne 0) { throw "manifest full-copy 失败" }

    # 2) local voice dependencies + model (the whole point of the full pack)
    Write-Host "  复制 $SttLib ..."
    Copy-Item $SttLib (Join-Path $stage "stt_lib") -Recurse -Force
    Write-Host "  复制 $SttModels ..."
    # T10a/T10b: copy the whisper model but drop HuggingFace cache JUNK
    # (.locks / trees / .agent_harnesses.json / CACHEDIR.TAG). blobs/ +
    # snapshots/ + refs/ are the model itself and must be kept.
    $modelsDst = Join-Path $stage "stt_models"
    robocopy $SttModels $modelsDst /E /NFL /NDL /NJH /NJS /NP `
        /XD ".locks" "trees" /XF ".agent_harnesses.json" "CACHEDIR.TAG" | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy stt_models 失败 ($LASTEXITCODE)" }

    # 3) embedded Python runtime (must be provided; see FIRST_RUN.txt)
    $embed = Join-Path $root "python-embed"
    if (Test-Path $embed) {
        Copy-Item (Join-Path $embed "*") $stage -Recurse -Force
        # T10b: normalise the embedded ._pth so `import config`/`import lib`
        # work from the pack root (see tools/fix_embedded_pth.py).
        Get-ChildItem $stage -Filter "python*._pth" | ForEach-Object {
            py -3.12 (Join-Path $root "tools\fix_embedded_pth.py") $_.FullName
        }
    } else {
        Write-Warning "未找到 python-embed (全量包将不附带 embedded Python)。请下载 python-3.12.x-embed-amd64.zip 解压到 python-embed/ 后重试。"
    }

    # 4) launchers: ASCII start.bat (avoids unzip mojibake) + 启动.bat
    if (Test-Path (Join-Path $root "start.bat")) {
        Copy-Item (Join-Path $root "start.bat") (Join-Path $stage "start.bat") -Force
    }
    Copy-Item (Join-Path $root "启动.bat") (Join-Path $stage "启动.bat") -Force

    # 5) zip via Python zipfile (T10a: Compress-Archive OOMs on the full tree).
    #    Never bundle .env / sessions/ / __pycache__ / HF cache junk.
    $zip = Join-Path $dist "F1Engineer-full-$Version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "  压缩中（约 700MB，可能需几分钟）..."
    py -3.12 (Join-Path $root "tools\make_zip.py") $zip $stage
    Write-Host "-> $zip" -ForegroundColor Green
    Get-FileHash $zip -Algorithm SHA256 | ForEach-Object { "$($_.Hash)  $(Split-Path $zip -Leaf)" } |
        Add-Content (Join-Path $dist "SHA256SUMS.txt")
}

if (-not $FullOnly) { Build-Core }
if (-not $CoreOnly) { Build-Full }

Write-Host ""
Write-Host "完成。产物在 dist/ 目录" -ForegroundColor Green
Get-ChildItem $dist | Select-Object Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
