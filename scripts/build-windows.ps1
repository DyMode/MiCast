# Build the MiCast Windows single-file app.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-windows.ps1
#
# Steps: build the web UI, fetch UPX for binary compression, run PyInstaller.
# Output: dist\MiCast.exe. No ffmpeg download — transcoding is in-process
# (PyAV bundles libavcodec/libavfilter).

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# A running MiCast.exe locks dist\MiCast.exe and makes PyInstaller fail at the
# final copy step.
Get-Process MiCast -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false

function Assert-ExitCode($step) {
    if ($LASTEXITCODE -ne 0) { throw "$step failed with exit code $LASTEXITCODE" }
}

# 1. Web UI
Write-Host "==> Building web UI"
Push-Location web
npm run build
Assert-ExitCode "web build"
Pop-Location

# 2. UPX for binary compression (spec reads MICAST_UPX_DIR; MICAST_UPX=0 skips)
$upxDir = Join-Path $root "build-tools\upx"
if (-not (Test-Path (Join-Path $upxDir "upx.exe"))) {
    Write-Host "==> Downloading UPX"
    $upxZip = Join-Path $env:TEMP "upx.zip"
    if (Test-Path $upxZip) { Remove-Item -Force $upxZip }
    curl.exe -fSL --connect-timeout 15 "https://github.com/upx/upx/releases/download/v5.0.2/upx-5.0.2-win64.zip" -o $upxZip
    Assert-ExitCode "UPX download"
    $upxExtract = Join-Path $env:TEMP "upx-release"
    if (Test-Path $upxExtract) { Remove-Item -Recurse -Force $upxExtract }
    Expand-Archive $upxZip $upxExtract
    $upxExe = Get-ChildItem $upxExtract -Recurse -Filter upx.exe | Select-Object -First 1
    if (-not $upxExe) { throw "upx.exe not found in downloaded archive" }
    New-Item -ItemType Directory -Force $upxDir | Out-Null
    Copy-Item $upxExe.FullName (Join-Path $upxDir "upx.exe")
    Remove-Item -Force $upxZip
    Remove-Item -Recurse -Force $upxExtract
}
$env:MICAST_UPX_DIR = $upxDir

# 3. Desktop deps (not in requirements.txt — Docker/NAS images don't need them)
Write-Host "==> Installing desktop dependencies"
& (Join-Path $root ".venv\Scripts\python.exe") -m pip install -q pywebview pystray pywin32

# 4. PyInstaller. App and tray icons come from assets/icons, generated from
# assets/brand-approved/micast.svg. UPX is enabled in the spec; --upx-dir is how PyInstaller 6
# locates the binary (an upx_dir spec kwarg is silently ignored).
Write-Host "==> Running PyInstaller"
$pyinstallerArgs = @("--noconfirm", "--clean")
if ($env:MICAST_UPX -ne "0") { $pyinstallerArgs += @("--upx-dir", $upxDir) }
$pyinstallerArgs += "packaging\micast.spec"
& (Join-Path $root ".venv\Scripts\python.exe") -m PyInstaller @pyinstallerArgs
Assert-ExitCode "PyInstaller"

Write-Host ""
Write-Host ("Done: dist\MiCast.exe  ({0:N1} MB)" -f ((Get-Item dist\MiCast.exe).Length / 1MB))
Write-Host "Smoke test: dist\MiCast.exe  (opens http://localhost:42300/app/micast/)"
