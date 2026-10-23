# Repair WeChat Hook: kill WeChat -> restart -> wait for DB -> start autoreply
# Usage: powershell -ExecutionPolicy Bypass -File repair_hook.ps1

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$WxDir = "D:\wx"
$WxExe = Join-Path $WxDir "Weixin.exe"
$Dll   = Join-Path $WxDir "version.dll"
$Agent = "E:\AIagent"

function Test-HookReady {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:30001/QueryDB/execute" -Method Post `
            -Body '{"optDbName":"session.db","SQL":"SELECT 1"}' `
            -ContentType "application/json; charset=utf-8" -TimeoutSec 5
        return ($r.status -eq 0)
    } catch { return $false }
}

Write-Host "== Repair WeChat Hook ==" -ForegroundColor Cyan

if (-not (Test-Path $WxExe)) { Write-Host "[X] Not found: $WxExe" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $Dll))   { Write-Host "[X] Not found: $Dll (copy version.dll to WeChat folder)" -ForegroundColor Red; exit 1 }

Write-Host "[..] Stop autoreply..." -ForegroundColor Yellow
& "$Agent\autoreply_service.ps1" stop 2>$null

Write-Host "[..] Kill all Weixin..." -ForegroundColor Yellow
Get-Process -Name Weixin -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

Write-Host "[..] Start WeChat..." -ForegroundColor Yellow
Start-Process $WxExe -WorkingDirectory $WxDir
Write-Host ""
Write-Host ">>> Please LOG IN to WeChat in the window, then press Enter here <<<" -ForegroundColor Green
Read-Host

Write-Host "[..] Waiting for Hook DB (up to 90s)..." -ForegroundColor Yellow
$ok = $false
for ($i = 1; $i -le 30; $i++) {
    if (Test-HookReady) { $ok = $true; Write-Host "[OK] Hook ready (${i}*3s)" -ForegroundColor Green; break }
    Start-Sleep -Seconds 3
    Write-Host "  ... waiting ($i/30)"
}
if (-not $ok) {
    Write-Host "[X] Hook still not ready. Check version.dll matches WeChat version." -ForegroundColor Red
    exit 1
}

Write-Host "[..] Start autoreply..." -ForegroundColor Yellow
& "$Agent\autoreply_service.ps1" start

Write-Host "[..] Restart web panel..." -ForegroundColor Yellow
& "$Agent\web_panel_service.ps1" restart

Write-Host "[OK] Done. Open http://127.0.0.1:8848" -ForegroundColor Green
