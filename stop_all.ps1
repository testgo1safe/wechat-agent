# 完整停止本项目相关进程：自动回复、管理面板、启动器、端口占用
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File stop_all.ps1
#        stop_all.bat

$ErrorActionPreference = "SilentlyContinue"
$Dir = $PSScriptRoot
$PidFile = Join-Path $Dir "autoreply.pid"
$PanelPorts = @(8848, 8849)
if ($env:WECHAT_WEB_PORT) {
    try { $PanelPorts = @([int]$env:WECHAT_WEB_PORT) + $PanelPorts | Select-Object -Unique } catch {}
}

$ScriptMarkers = @(
    "wechat_autoreply.py",
    "wechat_web.py",
    "autoreply_hidden.vbs",
    "autoreply_daemon.bat",
    "web_panel.bat"
)

function Say($msg, $color = "White") {
    Write-Host $msg -ForegroundColor $color
}

function Get-AllCandidateProcs {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $n = [string]$_.Name
            $cmd = [string]$_.CommandLine
            if ($n -match '^(pythonw?|py)\.exe$') { return $true }
            if ($n -match '^(wscript|cscript)\.exe$' -and $cmd -like '*autoreply_hidden.vbs*') { return $true }
            if ($n -match '^(cmd|powershell|pwsh)\.exe$' -and (
                    $cmd -like '*wechat_autoreply.py*' -or
                    $cmd -like '*wechat_web.py*' -or
                    $cmd -like '*autoreply_daemon.bat*' -or
                    $cmd -like '*web_panel.bat*' -or
                    $cmd -like '*autoreply_service.ps1* start*' -or
                    $cmd -like '*web_panel_service.ps1* start*'
                )) { return $true }
            return $false
        })
}

function Test-CmdMatchesProject($cmd) {
    if ([string]::IsNullOrWhiteSpace($cmd)) { return $false }
    foreach ($m in $ScriptMarkers) {
        if ($cmd -like "*$m*") { return $true }
    }
    $dirNorm = $Dir.TrimEnd('\')
    if ($cmd -like "*$dirNorm*" -and (
            $cmd -like '*wechat_autoreply*' -or
            $cmd -like '*wechat_web*' -or
            $cmd -like '*autoreply*'
        )) { return $true }
    return $false
}

function Get-PidsListeningOnPort([int]$Port) {
    $pids = @{}
    $lines = netstat -ano 2>$null
    if (-not $lines) { return @() }
    $re = [regex]"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|\[::\]):$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($line in $lines) {
        $m = $re.Match([string]$line)
        if ($m.Success) {
            $id = [int]$m.Groups[1].Value
            if ($id -gt 0) { $pids[$id] = $true }
        }
    }
    return @($pids.Keys)
}

function Get-ProjectProcs {
    $found = @{}
    foreach ($p in Get-AllCandidateProcs) {
        $cmd = [string]$p.CommandLine
        if (Test-CmdMatchesProject $cmd) {
            $found[$p.ProcessId] = [pscustomobject]@{
                ProcessId   = $p.ProcessId
                Name        = $p.Name
                CommandLine = $cmd
                Source      = "cmdline"
            }
        }
    }

    if (Test-Path $PidFile) {
        $filePid = 0
        try {
            $filePid = [int](Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1)
        } catch { $filePid = 0 }
        if ($filePid -gt 0) {
            $alive = Get-Process -Id $filePid -ErrorAction SilentlyContinue
            if ($alive -and $alive.ProcessName -match '^(pythonw?|py)$') {
                if (-not $found.ContainsKey($filePid)) {
                    $found[$filePid] = [pscustomobject]@{
                        ProcessId   = $filePid
                        Name        = "$($alive.ProcessName).exe"
                        CommandLine = "(from autoreply.pid)"
                        Source      = "pidfile"
                    }
                }
            }
        }
    }

    foreach ($port in $PanelPorts) {
        foreach ($listenPid in Get-PidsListeningOnPort $port) {
            $alive = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
            if (-not $alive) { continue }
            if ($alive.ProcessName -notmatch '^(pythonw?|py)$') { continue }
            if (-not $found.ContainsKey($listenPid)) {
                $found[$listenPid] = [pscustomobject]@{
                    ProcessId   = $listenPid
                    Name        = "$($alive.ProcessName).exe"
                    CommandLine = "(listening :$port)"
                    Source      = "port"
                }
            }
        }
    }

    return @($found.Values)
}

function Stop-PidSafe([int]$ProcId, $label) {
    if ($ProcId -le 0) { return $false }
    if ($ProcId -eq $PID) { return $false }
    $before = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $before) { return $false }
    try {
        Stop-Process -Id $ProcId -Force -ErrorAction Stop
    } catch {
        & taskkill.exe /PID $ProcId /F /T 2>$null | Out-Null
    }
    Start-Sleep -Milliseconds 200
    $after = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if ($after) {
        & taskkill.exe /PID $ProcId /F /T 2>$null | Out-Null
        Start-Sleep -Milliseconds 300
        $after = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    }
    if (-not $after) {
        Say "  [OK] stopped $label PID=$ProcId" "Green"
        return $true
    }
    Say "  [!] failed to stop $label PID=$ProcId" "Red"
    return $false
}

function Stop-ProjectRound($round) {
    $procs = @(Get-ProjectProcs)
    if ($procs.Count -eq 0) { return 0 }
    Say "Round $round : found $($procs.Count) process(es)" "Yellow"
    $n = 0
    foreach ($p in ($procs | Sort-Object ProcessId -Unique)) {
        $hint = $p.Source
        $short = ([string]$p.CommandLine)
        if ($short.Length -gt 90) { $short = $short.Substring(0, 87) + "..." }
        if (Stop-PidSafe $p.ProcessId "$($p.Name) [$hint] $short") { $n++ }
    }
    return $n
}

Say "========================================" "Cyan"
Say " Stop WeChat Local AI (full cleanup)" "Cyan"
Say "========================================" "Cyan"
Say "Dir: $Dir" "DarkGray"
Say ""

$svc = Join-Path $Dir "autoreply_service.ps1"
if (Test-Path $svc) {
    Say "[1] autoreply_service stop ..." "Cyan"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $svc stop
} else {
    Say "[1] skip autoreply_service (missing)" "DarkGray"
}

$wp = Join-Path $Dir "web_panel_service.ps1"
if (Test-Path $wp) {
    Say "[2] web_panel_service stop ..." "Cyan"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $wp stop
} else {
    Say "[2] skip web_panel_service (missing)" "DarkGray"
}

Say "[3] full scan leftovers ..." "Cyan"
for ($r = 1; $r -le 3; $r++) {
    $killed = Stop-ProjectRound $r
    if ($killed -eq 0) { break }
    Start-Sleep -Seconds 1
}

if (Test-Path $PidFile) {
    $pidFileProcess = $null
    try {
        $pidFileValue = [int](Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1)
        $pidFileProcess = Get-Process -Id $pidFileValue -ErrorAction SilentlyContinue
    } catch {}
    if ($pidFileProcess -and $pidFileProcess.ProcessName -match '^(pythonw?|py)$') {
        Say "  [!] kept autoreply.pid: PID=$pidFileValue is still running" "Red"
    } else {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Say "  [OK] removed autoreply.pid" "Green"
    }
}
$lock = Join-Path $env:TEMP "wechat_hook_global.lock"
if (Test-Path $lock) {
    try {
        Remove-Item $lock -Force -ErrorAction Stop
        Say "  [OK] removed wechat_hook_global.lock" "Green"
    } catch {}
}

Say ""
Say "[4] final verify ..." "Cyan"
$left = @(Get-ProjectProcs)
$portHits = @()
foreach ($port in $PanelPorts) {
    foreach ($listenPid in Get-PidsListeningOnPort $port) {
        $alive = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
        if ($alive -and $alive.ProcessName -match '^(pythonw?|py)$') {
            $portHits += [pscustomobject]@{ Port = $port; ProcessId = $listenPid; Name = $alive.ProcessName }
        }
    }
}

$ok = ($left.Count -eq 0) -and ($portHits.Count -eq 0)
if ($ok) {
    Say "All stopped. No autoreply / web panel / launcher left." "Green"
} else {
    Say "Leftovers found, cleaning again:" "Red"
    foreach ($p in $left) {
        Say ("  PID={0} {1} {2}" -f $p.ProcessId, $p.Name, $p.CommandLine) "Yellow"
    }
    foreach ($h in $portHits) {
        Say ("  port {0} held by PID={1} ({2})" -f $h.Port, $h.ProcessId, $h.Name) "Yellow"
        Stop-PidSafe $h.ProcessId "port:$($h.Port)" | Out-Null
    }
    Start-Sleep -Seconds 1
    $left2 = @(Get-ProjectProcs)
    if ($left2.Count -eq 0) {
        Say "Second pass: all stopped." "Green"
        $ok = $true
    } else {
        Say "Still $($left2.Count) process(es). Check Task Manager for python.exe." "Red"
    }
}

Say ""
$arLeft = @(Get-ProjectProcs | Where-Object {
        $_.CommandLine -like '*wechat_autoreply*' -or $_.Source -eq 'pidfile'
    })
$webLeft = @()
foreach ($port in $PanelPorts) {
    foreach ($listenPid in Get-PidsListeningOnPort $port) {
        $alive = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
        if ($alive -and $alive.ProcessName -match '^(pythonw?|py)$') {
            $webLeft += $listenPid
        }
    }
}
Say "----------------------------------------" "Cyan"
if ($arLeft.Count) {
    Say (" Autoreply : RUNNING PID={0}" -f ($arLeft.ProcessId -join ',')) "Yellow"
} else {
    Say " Autoreply : stopped" "Green"
}
if ($webLeft.Count) {
    Say (" Web panel : PORT BUSY PID={0}" -f ($webLeft -join ',')) "Yellow"
} else {
    Say " Web panel : stopped" "Green"
}
Say "----------------------------------------" "Cyan"

if ($ok) { exit 0 } else { exit 1 }
