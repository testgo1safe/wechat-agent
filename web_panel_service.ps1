# Web panel service helper
# Usage:
#   powershell -ExecutionPolicy Bypass -File web_panel_service.ps1 start
#   powershell -ExecutionPolicy Bypass -File web_panel_service.ps1 stop
#   powershell -ExecutionPolicy Bypass -File web_panel_service.ps1 restart

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action
)

$Dir = $PSScriptRoot
$Port = 8848
if ($env:WECHAT_WEB_PORT) { $Port = [int]$env:WECHAT_WEB_PORT }
$Script = Join-Path $Dir "wechat_web.py"
$PortablePython = Join-Path $Dir "runtime\python\python.exe"
$PythonExe = if (Test-Path $PortablePython) { $PortablePython } else { "python" }

function Get-WebProcs {
    $found = @{}
    foreach ($name in @("python.exe", "pythonw.exe", "py.exe")) {
        Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*wechat_web.py*" } |
            ForEach-Object { $found[$_.ProcessId] = $_ }
    }
    # 端口占用兜底（cmdline 为空时）
    $ports = @($Port)
    if ($Port -ne 8849) { $ports += 8849 }
    foreach ($listenPort in $ports) {
        $lines = netstat -ano 2>$null | Select-String "LISTENING"
        foreach ($line in $lines) {
            if ($line -notmatch ":$listenPort\s+") { continue }
            if ($line -match "\s(\d+)\s*$") {
                $listenPid = [int]$Matches[1]
                $proc = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -match '^(pythonw?|py)$' -and -not $found.ContainsKey($listenPid)) {
                    $found[$listenPid] = [pscustomobject]@{
                        ProcessId   = $listenPid
                        CommandLine = "(listening :$listenPort)"
                    }
                }
            }
        }
    }
    return @($found.Values)
}

function Stop-WebPanel {
    $procs = Get-WebProcs
    if (-not $procs) {
        Write-Host "Web panel not running."
        return
    }
    $procs | ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "Stopped PID $($_.ProcessId)"
        } catch {
            Write-Host "[!] Could not stop PID $($_.ProcessId): $_" -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds 1
}

function Test-PortListening([int]$Port) {
    $hits = netstat -ano 2>$null | Select-String "127.0.0.1:$Port\s"
    return [bool]$hits
}

function Start-WebPanel {
    if (Get-WebProcs) {
        Write-Host "Web panel already running."
        return
    }
    if (Test-PortListening $Port) {
        Write-Host "[!] Port $Port already in use (stale process?). Try: restart or set WECHAT_WEB_PORT=8849" -ForegroundColor Yellow
        $Port = 8849
        if (Test-PortListening $Port) {
            Write-Host "[!] Port 8849 also busy. Stop the old process manually." -ForegroundColor Red
            return
        }
        Write-Host "Fallback to port $Port"
    }
    Start-Process -FilePath $PythonExe -ArgumentList "-u `"$Script`" --port $Port" `
        -WorkingDirectory $Dir -WindowStyle Hidden
    Start-Sleep -Seconds 2
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Host "Web panel started: http://127.0.0.1:$Port"
    } catch {
        Write-Host "Web panel may still be starting: http://127.0.0.1:$Port"
    }
}

switch ($Action) {
    "start"   { Start-WebPanel }
    "stop"    { Stop-WebPanel }
    "restart" { Stop-WebPanel; Start-Sleep -Seconds 1; Start-WebPanel }
    "status"  {
        $p = Get-WebProcs
        if ($p) { Write-Host "Running PID=$($p.ProcessId -join ',')" }
        else { Write-Host "Not running" }
    }
}
