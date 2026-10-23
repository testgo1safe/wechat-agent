# One background waiter per Windows session. Exit when stopped or Hook is ready.
$ErrorActionPreference = "Continue"
$Dir = $PSScriptRoot
$StopFile = Join-Path $Dir "autoreply.stop"
$ServiceScript = Join-Path $Dir "autoreply_service.ps1"
$LogFile = Join-Path $Dir "portable_bootstrap.log"
$Mutex = New-Object System.Threading.Mutex($false, "Local\WeChatAutoReplyPortableHookWait")
$owns = $false

try {
    $owns = $Mutex.WaitOne(0)
    if (-not $owns) { exit 0 }
    while (-not (Test-Path -LiteralPath $StopFile)) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:30001/QueryDB/status" -TimeoutSec 2
            if ([uint64]$status.hWeixin -ne 0) {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ServiceScript start |
                    Out-File -FilePath $LogFile -Append -Encoding UTF8
                $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                    Where-Object { [string]$_.CommandLine -like "*$Dir*wechat_autoreply.py*" })
                if ($running.Count -gt 0) { exit 0 }
            }
        } catch {}
        Start-Sleep -Seconds 3
    }
} finally {
    if ($owns) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
