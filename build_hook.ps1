# Rebuild WeChat Hook version.dll (enable SendImgMsg + Decode_Pic)
# Usage: powershell -ExecutionPolicy Bypass -File build_hook.ps1

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$HookDir = Join-Path $PSScriptRoot "WeChat-Hook"
$Slx = Join-Path $HookDir "x64_Version_dll.sln"
$OutDll = Join-Path $HookDir "x64\Release\version.dll"
$WxDir = "D:\wx"
$Target = Join-Path $WxDir "version.dll"

if (-not (Test-Path $Slx)) {
    Write-Host "[X] Solution not found: $Slx" -ForegroundColor Red
    exit 1
}

$msbuild = @(
    "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
    "${env:ProgramFiles}\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
    "${env:ProgramFiles}\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
    "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $msbuild) {
    Write-Host "[X] MSBuild not found. Install VS 2022 with C++ workload." -ForegroundColor Red
    exit 1
}

Write-Host "== Build version.dll ==" -ForegroundColor Cyan
& $msbuild $Slx /p:Configuration=Release /p:Platform=x64 /m
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path $OutDll)) {
    Write-Host "[X] Build output missing: $OutDll" -ForegroundColor Red
    exit 1
}

if (Test-Path $WxDir) {
    Copy-Item $OutDll $Target -Force
    Write-Host "[OK] Copied to $Target" -ForegroundColor Green
    Write-Host "     Restart WeChat (repair_hook.ps1) to load new DLL." -ForegroundColor Yellow
} else {
    Write-Host "[OK] Built: $OutDll" -ForegroundColor Green
    Write-Host "     Copy manually to WeChat folder." -ForegroundColor Yellow
}
