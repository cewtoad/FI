# Runs INSIDE Windows Sandbox (as WDAGUtilityAccount) to validate the F1_TR
# release packs on a clean Windows with no Python / no VC++ preinstalled.
#
# The sandbox maps:
#   Z:\in   -> host dist/            (read-only; contains the .zip packs)
#   Z:\out  -> host sandbox_out/     (writable; the report lands here)
#
# It extracts each pack into a fresh location (one with Chinese + spaces in the
# path to exercise that), runs FI.py --selftest, checks imports, and writes a
# plain-text report to Z:\out\sandbox_report.txt.

$ErrorActionPreference = "Continue"
$report = "Z:\out\sandbox_report.txt"
"F1_TR sandbox validation - $(Get-Date)" | Out-File $report -Encoding utf8

function Log($m) { $m | Tee-Object -FilePath $report -Append }

Log "== environment =="
Log ("OS: " + (Get-CimInstance Win32_OperatingSystem).Caption + " " + (Get-CimInstance Win32_OperatingSystem).Version)
Log ("python on PATH: " + ([bool](Get-Command python -ErrorAction SilentlyContinue)))
Log ("py launcher:    " + ([bool](Get-Command py -ErrorAction SilentlyContinue)))

# Chinese + space path to exercise non-ASCII / spaces.
$work = "C:\Users\WDAGUtilityAccount\Desktop\赛车 工具\F1"
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Test-Pack($zipName, $label) {
    Log ""
    Log "== $label ($zipName) =="
    $zip = "Z:\in\$zipName"
    if (-not (Test-Path $zip)) { Log "  MISSING: $zip"; return }
    $dest = Join-Path $work $label
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    try { Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force }
    catch { Log "  extract FAILED: $_"; return }
    $root = Join-Path $dest "F1Engineer"
    Log "  extracted to: $root"

    # embedded python (full pack) vs bundled exe (core)
    $py = Join-Path $root "python.exe"
    if (Test-Path $py) {
        $out = & $py -c "import config, lib; from lib.f1_types import F1PacketType; print('imports OK', len(list(F1PacketType)))" 2>&1
        Log ("  embedded import: " + ($out -join " | "))
        $out = & $py FI.py --selftest 2>&1
        $out | Out-File (Join-Path $work "$label-selftest.txt") -Encoding utf8
        $res = ($out | Select-String "结果|passed|PASS").Line -join " ; "
        Log ("  selftest: " + $res)
    }
    $exe = Join-Path $root "F1Engineer.exe"
    if (Test-Path $exe) {
        $out = & $exe --selftest 2>&1
        $res = ($out | Select-String "结果|PASS|FAIL").Line -join " ; "
        Log ("  exe selftest: " + $res)
    }
    Log ("  start.bat present: " + (Test-Path (Join-Path $root 'start.bat')))
}

Test-Pack "F1Engineer-core-0.4.0-win64.zip" "core"
Test-Pack "F1Engineer-full-0.4.0-win64.zip" "full"

Log ""
Log "== read-only dir test =="
$ro = Join-Path $work "readonly"
New-Item -ItemType Directory -Force -Path $ro | Out-Null
Copy-Item (Join-Path $work "core\F1Engineer\*") $ro -Recurse -Force
icacls $ro /deny "WDAGUtilityAccount:(W,D)" /T | Out-Null
$exe = Join-Path $ro "F1Engineer.exe"
$out = & $exe --selftest 2>&1
Log ("  read-only exe rc=$LASTEXITCODE; wrote .env? " + (Test-Path (Join-Path $ro '.env')))
icacls $ro /remove:d "WDAGUtilityAccount" /T | Out-Null

Log ""
Log "DONE. Report at Z:\out\sandbox_report.txt"
