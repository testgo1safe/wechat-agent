# Start the bundled WeChat, local panel and auto-reply service.
param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$WeChatDir = Join-Path $Dir "WeChat"
$WeChatExe = Join-Path $WeChatDir "Weixin.exe"
$HookDll = Join-Path $WeChatDir "version.dll"
$PythonExe = Join-Path $Dir "runtime\python\python.exe"
$ConfigFile = Join-Path $Dir "autoreply_config.json"
$StopFile = Join-Path $Dir "autoreply.stop"
$PanelScript = Join-Path $Dir "web_panel_service.ps1"
$ServiceScript = Join-Path $Dir "autoreply_service.ps1"
$WaitScript = Join-Path $Dir "portable_hook_wait.ps1"

foreach ($path in @($WeChatExe, $HookDll, $PythonExe, $ConfigFile,
                   $PanelScript, $ServiceScript, $WaitScript)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Host "[X] Missing: $path" -ForegroundColor Red
        exit 1
    }
}

$expected = [IO.Path]::GetFullPath($WeChatExe)
$runningWeChat = @(Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" -ErrorAction SilentlyContinue)
foreach ($proc in $runningWeChat) {
    $actual = [string]$proc.ExecutablePath
    if (-not $actual) {
        Write-Host "[X] Cannot verify the path of running WeChat PID $($proc.ProcessId)." -ForegroundColor Red
        Write-Host "Close it, then start this portable copy again." -ForegroundColor Yellow
        exit 1
    }
    if ($actual -and
        -not [string]::Equals([IO.Path]::GetFullPath($actual), $expected,
                             [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "[X] Another WeChat is already running: $actual" -ForegroundColor Red
        Write-Host "Close it, then start this portable copy again." -ForegroundColor Yellow
        exit 1
    }
}

if (-not $runningWeChat) {
    Start-Process -FilePath $WeChatExe -WorkingDirectory $WeChatDir
    Write-Host "[OK] Bundled WeChat started. Please scan QR code if asked." -ForegroundColor Green
} else {
    Write-Host "[OK] Bundled WeChat is already running." -ForegroundColor Green
}

if (Test-Path -LiteralPath $StopFile) {
    Remove-Item -LiteralPath $StopFile -Force
}

& $PanelScript start
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8848"
}

$ready = $false
try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:30001/QueryDB/status" -TimeoutSec 2
    $ready = ([uint64]$status.hWeixin -ne 0)
} catch {}

if ($ready) {
    & $ServiceScript start
    Write-Host "[OK] Auto-reply service start requested." -ForegroundColor Green
} else {
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", "`"$WaitScript`"") `
        -WorkingDirectory $Dir -WindowStyle Hidden
    Write-Host "[..] Waiting for WeChat login; auto-reply will start in the background." -ForegroundColor Yellow
}

Write-Host "Panel: http://127.0.0.1:8848"
Write-Host "Stop:  stop_all.bat"
