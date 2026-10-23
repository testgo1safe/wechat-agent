# Gemini image gen - Chrome debug launcher (Chrome 150+)
$ErrorActionPreference = "SilentlyContinue"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Profile = Join-Path $Dir "chrome_cdp_profile"
$Chrome = "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $Chrome)) {
    $Chrome = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
}

Write-Host "========================================"
Write-Host " Gemini - Chrome debug mode"
Write-Host "========================================"
Write-Host ""
Write-Host "Chrome 150+ blocks debug port on default profile."
Write-Host "Using profile folder: chrome_cdp_profile"
Write-Host ""
Write-Host "FIRST TIME: login gemini.google.com in the Chrome window that opens."
Write-Host ""

if (-not (Test-Path $Chrome)) {
    Write-Host "[ERROR] Google Chrome not found"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[1/3] Closing Chrome..."
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

if (-not (Test-Path $Profile)) { New-Item -ItemType Directory -Path $Profile | Out-Null }

Write-Host "[2/3] Starting Chrome (port 9222)..."
Start-Process -FilePath $Chrome -ArgumentList @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$Profile",
    "--remote-allow-origins=*",
    "--no-first-run",
    "https://gemini.google.com"
)

Write-Host "[3/3] Waiting for port 9222 (max 15s)..."
$ok = $false
1..15 | ForEach-Object {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:9222/json/version" -UseBasicParsing -TimeoutSec 1
        if ($r.StatusCode -eq 200) { $ok = $true; return }
    } catch {}
    Write-Host "  ...${_}s"
    Start-Sleep -Seconds 1
}

if (-not $ok) {
    Write-Host "[FAIL] Port 9222 not ready"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[OK] Port 9222 ready"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Login gemini.google.com in THIS Chrome window (if not yet)"
Write-Host "  2. Run:  cd $Dir"
Write-Host "           python gemini_web_setup.py"
Write-Host ""
Write-Host "Keep using THIS Chrome for image gen. Do not use desktop shortcut."
Read-Host "Press Enter to exit"
