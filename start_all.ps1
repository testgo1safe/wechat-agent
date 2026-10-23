# One-click launcher for WeChat local AI auto-reply stack.
# Usage: powershell -ExecutionPolicy Bypass -File start_all.ps1

param(
    [switch]$NoBrowser,
    [switch]$NoAutoreply
)

$ErrorActionPreference = "Continue"
$Dir = $PSScriptRoot
$Config = Join-Path $Dir "autoreply_config.json"
$Example = Join-Path $Dir "autoreply_config.example.json"
$LogFile = Join-Path $Dir "autoreply.log"
$PidFile = Join-Path $Dir "autoreply.pid"
$PanelPort = 8848
$HookUrl = "http://127.0.0.1:30001"
$PanelUrl = "http://127.0.0.1:$PanelPort"
$PortablePython = Join-Path $Dir "runtime\python\python.exe"
$PythonExe = if (Test-Path $PortablePython) {
    $PortablePython
} else {
    (Get-Command python -ErrorAction SilentlyContinue).Source
}

function Say($msg, $color = "White") {
    Write-Host $msg -ForegroundColor $color
}

function Test-HookReady($maxWaitSec = 30) {
    $deadline = (Get-Date).AddSeconds($maxWaitSec)
    while ((Get-Date) -lt $deadline) {
        try {
            # 就绪探测只读进程状态，绝不能为了“心跳”反复执行真实 SQL。
            $r = Invoke-RestMethod -Uri "$HookUrl/QueryDB/status" -Method Get -TimeoutSec 3
            if ([uint64]$r.hWeixin -ne 0) { return $true }
        } catch {
            $null
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-AutoReplyProcs {
    $byCmd = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*wechat_autoreply.py*" })
    if ($byCmd) { return $byCmd }
    if (Test-Path $PidFile) {
        try {
            $filePid = [int](Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1)
        } catch { $filePid = 0 }
        if ($filePid -gt 0) {
            $p = Get-Process -Id $filePid -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -eq "python") {
                return @([PSCustomObject]@{ ProcessId = $filePid })
            }
        }
    }
    return @()
}

function Test-AutoReplyRunning {
    return [bool](Get-AutoReplyProcs)
}

function Show-LogTail($n = 10) {
    if (-not (Test-Path $LogFile)) { return }
    $py = $PythonExe
    if ($py) {
        & $py -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); p=r'$LogFile'; xs=open(p,encoding='utf-8-sig',errors='replace').read().splitlines(); n=$n; print(chr(10).join(xs[-n:]))"
        return
    }
    Get-Content $LogFile -Tail $n -Encoding UTF8 -ErrorAction SilentlyContinue
}

function Start-AutoReplyDaemon {
    $svc = Join-Path $Dir "autoreply_service.ps1"
    if (-not (Test-Path $svc)) {
        Say "[X] Missing autoreply_service.ps1" "Red"
        return $false
    }
    powershell -NoProfile -ExecutionPolicy Bypass -File $svc restart | Out-Host
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-AutoReplyRunning) { return $true }
    }
    return $false
}

function Get-WebPanelProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*wechat_web.py*" }
}

Say "========================================" "Cyan"
Say " WeChat Local AI - One Click Start" "Cyan"
Say "========================================" "Cyan"
Say ""

if (-not $PythonExe) {
    Say "[X] Python not found. Install Python 3.10+ first." "Red"
    exit 1
}
Say "[OK] Python: $PythonExe" "Green"

if (-not (Test-Path $Config)) {
    if (Test-Path $Example) {
        Copy-Item $Example $Config
        Say "[!] Created autoreply_config.json from example (dry-run by default)" "Yellow"
    } else {
        Say "[X] Missing autoreply_config.json" "Red"
        exit 1
    }
} else {
    Say "[OK] Config: autoreply_config.json" "Green"
}

$autoReplyEnabled = $true
try {
    $cfgPreview = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -ne $cfgPreview.enabled) {
        $autoReplyEnabled = [bool]$cfgPreview.enabled
    }
} catch {
    Say "[!] Could not read config enabled flag; using enabled=true" "Yellow"
}

if ($cfgPreview.llm.provider -eq "ollama" -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:11434" -UseBasicParsing -TimeoutSec 2 | Out-Null
        Say "[OK] Ollama is running" "Green"
    } catch {
        Say "[!] Ollama installed but not running. Start: ollama serve" "Yellow"
    }
} elseif ($cfgPreview.llm.provider -eq "ollama") {
    Say "[!] Ollama not in PATH. Install from https://ollama.com" "Yellow"
}

Say "[..] Waiting for WeChat Hook (up to 30s)..." "Yellow"
$hookReady = $false
if (Test-HookReady 30) {
    Say "[OK] WeChat Hook ready ($HookUrl)" "Green"
    $hookReady = $true
} else {
    Say "[!] WeChat Hook not ready. Login WeChat and check version.dll in WeChat folder." "Yellow"
    Say "    Example: D:\wx\version.dll beside Weixin.exe, then restart WeChat" "Yellow"
}

Say ""

if (-not $NoAutoreply) {
    if (-not $autoReplyEnabled) {
        Say "[!] Auto-reply is disabled by autoreply_config.json (enabled=false)." "Yellow"
    } elseif (-not $hookReady) {
        Say "[!] Skip auto-reply start (hook not ready). Fix hook then rerun." "Yellow"
    } elseif (Test-AutoReplyRunning) {
        $ar = @(Get-AutoReplyProcs)
        Say ("[OK] Auto-reply already running (PID {0})" -f $ar[0].ProcessId) "Green"
    } elseif (Start-AutoReplyDaemon) {
        $ar = @(Get-AutoReplyProcs)
        $procId = if ($ar.Count -gt 0) { $ar[0].ProcessId } else { "?" }
        Say "[OK] Auto-reply running (PID $procId)" "Green"
    } else {
        Say "[X] Auto-reply failed to start. Latest log:" "Red"
        Show-LogTail 10 | ForEach-Object { Say "    $_" "DarkGray" }
        $errLog = Join-Path $Dir "autoreply_stderr.log"
        if (Test-Path $errLog) {
            Say "    --- stderr ---" "DarkGray"
            Get-Content $errLog -Tail 3 -Encoding UTF8 -ErrorAction SilentlyContinue |
                ForEach-Object { Say "    $_" "DarkGray" }
        }
        Say "    Try: autoreply_start.bat" "Yellow"
    }
}

$wpScript = Join-Path $Dir "web_panel_service.ps1"
if (Test-Path $wpScript) {
    & $wpScript start
}
$wp = @(Get-WebPanelProcs)
if ($wp.Count -gt 0) {
    Say "[OK] Web panel running (PID $($wp[0].ProcessId)): $PanelUrl" "Green"
} else {
    Say "[!] Web panel failed to start. Run: web_panel_service.ps1 restart" "Yellow"
}

Say ""
Say "----------------------------------------" "Cyan"
Say " Panel:    $PanelUrl" "White"
Say " Hook:     $HookUrl" "White"
Say " Log:      $LogFile" "White"
Say " Stop all: stop_all.bat" "White"
Say "----------------------------------------" "Cyan"

if (-not $NoBrowser) {
    Start-Process $PanelUrl
}

Say ""
Say "Done. Manage whitelist in the web panel." "Green"
