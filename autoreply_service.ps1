# WeChat auto-reply background service manager
# Usage:
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 start      # start hidden in background
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 stop       # stop
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 status     # show status
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 log        # follow log
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 install    # register auto-start at logon
#   powershell -ExecutionPolicy Bypass -File autoreply_service.ps1 uninstall  # remove auto-start

param([Parameter(Position=0)][string]$Action = "status")

$ErrorActionPreference = "SilentlyContinue"
$Dir      = $PSScriptRoot
$Vbs      = Join-Path $Dir "autoreply_hidden.vbs"
$LogFile  = Join-Path $Dir "autoreply.log"
$PidFile  = Join-Path $Dir "autoreply.pid"
$StopFile = Join-Path $Dir "autoreply.stop"
$HealthFile = Join-Path $Dir "autoreply_health.json"
$TaskName = "WeChatAutoReply"
$StartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "WeChatAutoReply.lnk"

function Get-AutoReplyProcs {
    $found = @{}
    $names = @("python.exe", "pythonw.exe", "py.exe")
    foreach ($name in $names) {
        Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*wechat_autoreply.py*" } |
            ForEach-Object {
                $found[$_.ProcessId] = [pscustomobject]@{
                    ProcessId   = $_.ProcessId
                    CommandLine = [string]$_.CommandLine
                }
            }
    }
    if (Test-Path $PidFile) {
        $filePid = 0
        try {
            $filePid = [int](Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1)
        } catch {
            $filePid = 0
        }
        if ($filePid -gt 0) {
            $p = Get-Process -Id $filePid -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -match '^(pythonw?|py)$') {
                if (-not $found.ContainsKey($filePid)) {
                    $found[$filePid] = [pscustomobject]@{
                        ProcessId   = $filePid
                        CommandLine = "wechat_autoreply.py (pidfile)"
                    }
                }
            } elseif (-not $p) {
                Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
    return @($found.Values)
}

function Test-AutoReplyRunning {
    return [bool](Get-AutoReplyProcs)
}

function Resolve-PythonExe {
    $portable = Join-Path $Dir "runtime\python\python.exe"
    if (Test-Path $portable) { return $portable }
    foreach ($c in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Start-AutoReplyPython {
    if (-not (Resolve-PythonExe)) { return $false }
    Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
    # 某些受限启动器会注入 127.0.0.1:9 作为“禁网代理”。若后台服务继承它，
    # Gemini 识图/生图会全部误连本机 9 端口。只清除此明确无效的哨兵值，
    # 用户真实配置的公司代理/本地代理保持不变。
    $proxyNames = @("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY")
    $savedProxy = @{}
    foreach ($name in $proxyNames) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($value -and $value -match '^(?i)(https?|socks5?)://(127\.0\.0\.1|localhost):9/?$') {
            $savedProxy[$name] = $value
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        }
    }
    try {
        Start-Process -FilePath "wscript.exe" `
            -ArgumentList @($Vbs) -WorkingDirectory $Dir -WindowStyle Hidden
    } finally {
        foreach ($name in $savedProxy.Keys) {
            [Environment]::SetEnvironmentVariable(
                $name, [string]$savedProxy[$name], "Process")
        }
    }
    return $true
}

function Show-Health {
    if (-not (Test-Path $HealthFile)) {
        Write-Host "Health: waiting for first probe" -ForegroundColor Yellow
        return
    }
    try {
        $h = Get-Content $HealthFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $age = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$h.updated_at)
        $good = @($h.fts_good).Count
        $bad = @($h.fts_bad).Count
        $primary = [bool]$h.message_sqlite_open
        $line = "Health: $($h.state), age=${age}s, hook=$($h.hook_ready), session=$($h.session_ok), message_db=$primary, backup_fts=$good/4"
        if ($age -gt 420 -or $h.state -eq "blocked") {
            Write-Host $line -ForegroundColor Red
        } elseif ($h.state -eq "degraded") {
            Write-Host $line -ForegroundColor Yellow
        } else {
            Write-Host $line -ForegroundColor Green
        }
    } catch {
        Write-Host "Health: unreadable" -ForegroundColor Yellow
    }
}

switch ($Action.ToLower()) {
    "start" {
        if (Test-AutoReplyRunning) {
            Get-AutoReplyProcs | ForEach-Object { Write-Host ("Already running PID={0}" -f $_.ProcessId) -ForegroundColor Yellow }
            break
        }
        if (-not (Start-AutoReplyPython)) {
            Write-Host "Start failed: python not found." -ForegroundColor Red
            break
        }
        $ok = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if (Test-AutoReplyRunning) { $ok = $true; break }
        }
        if ($ok) {
            Get-AutoReplyProcs | ForEach-Object { Write-Host ("Started PID={0}" -f $_.ProcessId) -ForegroundColor Green }
        } else {
            Write-Host "Start failed (waited 30s). See autoreply_stderr.log" -ForegroundColor Red
            $errLog = Join-Path $Dir "autoreply_stderr.log"
            if (Test-Path $errLog) {
                Get-Content $errLog -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
            }
        }
    }
    "stop" {
        Set-Content -LiteralPath $StopFile -Value "stop" -Encoding ASCII -Force
        $killed = $false
        for ($r = 1; $r -le 3; $r++) {
            $p = @(Get-AutoReplyProcs)
            if (-not $p) { break }
            $p | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                & taskkill.exe /PID $_.ProcessId /F /T 2>$null | Out-Null
                Write-Host ("Stopped PID={0}" -f $_.ProcessId) -ForegroundColor Green
                $killed = $true
            }
            Start-Sleep -Seconds 1
        }
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -match '^(wscript|cscript)\.exe$' -and
                 [string]$_.CommandLine -like "*autoreply_hidden.vbs*") -or
                ($_.Name -match '^(cmd|powershell|pwsh)\.exe$' -and
                 [string]$_.CommandLine -like "*autoreply_daemon.bat*")
            } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Host ("Stopped launcher PID={0}" -f $_.ProcessId) -ForegroundColor Green
                $killed = $true
            }
        $remaining = @(Get-AutoReplyProcs)
        if ($remaining.Count -gt 0) {
            # 可能是管理员权限启动的旧实例：普通用户可看见但无法终止。
            # 必须保留 PID 文件，否则 status/start 会误判“未运行”。
            foreach ($remainingProc in $remaining) {
                $remainingPid = $remainingProc.ProcessId
                Write-Host "Still running PID=$remainingPid (may require administrator rights to stop)." -ForegroundColor Yellow
            }
        } else {
            if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
            if ($killed) { Write-Host "Stopped." -ForegroundColor Green }
            else { Write-Host "Not running." -ForegroundColor Yellow }
        }
    }
    "restart" {
        & $PSCommandPath stop
        Start-Sleep -Seconds 2
        & $PSCommandPath start
    }
    "status" {
        $p = Get-AutoReplyProcs
        if ($p) { $p | ForEach-Object { Write-Host ("Running PID={0}" -f $_.ProcessId) -ForegroundColor Green } }
        else { Write-Host "Not running." -ForegroundColor Yellow }
        Show-Health
        schtasks /Query /TN $TaskName > $null 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Auto-start: scheduled task" -ForegroundColor Green
        } elseif (Test-Path $StartupLink) {
            Write-Host "Auto-start: user startup shortcut" -ForegroundColor Green
        } else {
            Write-Host "Auto-start: not registered" -ForegroundColor Yellow
        }
    }
    "log" {
        $tail = @()
        if (Test-Path $LogFile) { $tail += $LogFile }
        $runLog = Join-Path $Dir "autoreply_run.log"
        if (Test-Path $runLog) { $tail += $runLog }
        if ($tail) {
            foreach ($f in $tail) { Get-Content $f -Tail 20 -Encoding UTF8 }
            Get-Content $tail[-1] -Tail 40 -Wait -Encoding UTF8
        } else {
            Write-Host "No log yet: $LogFile" -ForegroundColor Yellow
        }
    }
    "install" {
        schtasks /Create /TN $TaskName /TR "wscript.exe `"$Vbs`"" /SC ONLOGON /RL LIMITED /F 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Registered logon auto-start: $TaskName" -ForegroundColor Green
        } else {
            try {
                $shell = New-Object -ComObject WScript.Shell
                $shortcut = $shell.CreateShortcut($StartupLink)
                $shortcut.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
                $shortcut.Arguments = "`"$Vbs`""
                $shortcut.WorkingDirectory = $Dir
                $shortcut.Description = "WeChat AI AutoReply Supervisor"
                $shortcut.Save()
                Write-Host "Registered user startup shortcut: $StartupLink" -ForegroundColor Green
            } catch {
                Write-Host "Register failed: $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
    "uninstall" {
        schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
        $taskRemoved = ($LASTEXITCODE -eq 0)
        $linkRemoved = $false
        if (Test-Path $StartupLink) {
            Remove-Item -LiteralPath $StartupLink -Force -ErrorAction SilentlyContinue
            $linkRemoved = -not (Test-Path $StartupLink)
        }
        if ($taskRemoved -or $linkRemoved) { Write-Host "Auto-start removed." -ForegroundColor Green }
        else { Write-Host "Not registered or delete failed." -ForegroundColor Yellow }
    }
    default { Write-Host "Unknown action: $Action  (start|stop|restart|status|log|install|uninstall)" -ForegroundColor Red }
}
