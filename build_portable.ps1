# Build a private, relocatable Windows folder from the current working setup.
param(
    [string]$Destination = "",
    [string]$WeChatSource = "D:\wx",
    [string]$PythonSource = ""
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $Destination) {
    $Destination = Join-Path $Root ("portable_release_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
}
$Target = [IO.Path]::GetFullPath($Destination)
$RootPrefix = $Root.TrimEnd('\') + '\'
if (-not $Target.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must be a new folder inside $Root"
}
if (Test-Path -LiteralPath $Target) {
    throw "Destination already exists: $Target"
}

if (-not $PythonSource) {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $PythonSource = Split-Path -Parent $pythonCommand.Source
}
$PythonSource = [IO.Path]::GetFullPath($PythonSource)
$WeChatSource = [IO.Path]::GetFullPath($WeChatSource)
$WeChatExe = Join-Path $WeChatSource "Weixin.exe"
$HookDll = Join-Path $WeChatSource "version.dll"
$WeChatCore = Join-Path $WeChatSource "4.1.9.52\Weixin.dll"
$PythonExe = Join-Path $PythonSource "python.exe"
foreach ($required in @($WeChatExe, $HookDll, $WeChatCore, $PythonExe)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required file missing: $required"
    }
}
foreach ($entry in @($WeChatExe, $WeChatCore)) {
    $version = (Get-Item -LiteralPath $entry).VersionInfo.FileVersion
    if ($version -ne "4.1.9.52") {
        throw "Unsupported WeChat version at $entry : $version"
    }
}

$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        [string]$_.CommandLine -like "*$Root*wechat_autoreply.py*"
    })
if ($running.Count -gt 0) {
    throw "Stop the current auto-reply service before copying live memory databases."
}

function Copy-Tree([string]$From, [string]$To, [string[]]$Extra = @()) {
    if (-not (Test-Path -LiteralPath $From -PathType Container)) { return }
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    $copyArgs = @($From, $To, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS") + $Extra
    & robocopy.exe @copyArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Copy failed: $From -> $To (robocopy $LASTEXITCODE)"
    }
}

New-Item -ItemType Directory -Path $Target | Out-Null
Write-Host "Building $Target"

# Project sources and launchers. Runtime logs and full chat exports stay out.
$wantedNames = @(
    "requirements.txt", "autoreply_config.example.json",
    "gemini_cookies.example.json"
)
$sourceFiles = @(Get-ChildItem -LiteralPath $Root -File | Where-Object {
    $_.Extension -in @(".py", ".ps1", ".bat", ".vbs", ".js", ".md") -or
    $_.Name -in $wantedNames
})
foreach ($file in $sourceFiles) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $Target $file.Name)
}
foreach ($privateFile in @("autoreply_config.json", "gemini_cookies.json",
                          "autoreply_state.json")) {
    $source = Join-Path $Root $privateFile
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $Target $privateFile)
    }
}
foreach ($dataDir in @("memory", "personality_reports", "sticker_cache",
                       "knowledge_base")) {
    Copy-Tree (Join-Path $Root $dataDir) (Join-Path $Target $dataDir)
}

$stagedConfig = Join-Path $Target "autoreply_config.json"
if (Test-Path -LiteralPath $stagedConfig) {
    $cfg = Get-Content -LiteralPath $stagedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.image_gen) {
        $cfg.image_gen.cookie_file = "gemini_cookies.json"
    }
    # Two-second idle polling reduces message detection delay without making
    # every loop query the heavy message shards.
    if ($null -ne $cfg.interval -and [double]$cfg.interval -gt 2.0) {
        $cfg.interval = 2.0
    }
    $json = $cfg | ConvertTo-Json -Depth 100
    [IO.File]::WriteAllText($stagedConfig, $json,
        (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "Copying WeChat 4.1.9.52 ..."
$wxTarget = Join-Path $Target "WeChat"
New-Item -ItemType Directory -Path $wxTarget | Out-Null
Copy-Item -LiteralPath $WeChatExe, $HookDll -Destination $wxTarget
Copy-Tree (Join-Path $WeChatSource "4.1.9.52") (Join-Path $wxTarget "4.1.9.52")

Write-Host "Copying Python runtime ..."
$runtimeTarget = Join-Path $Target "runtime\python"
Copy-Tree $PythonSource $runtimeTarget @(
    "/XD",
    (Join-Path $PythonSource "Doc"),
    (Join-Path $PythonSource "include"),
    (Join-Path $PythonSource "libs"),
    (Join-Path $PythonSource "Scripts")
)

$portablePython = Join-Path $runtimeTarget "python.exe"
Push-Location $Target
try {
    & $portablePython -c "import sys, requests, Crypto, win32api, browser_cookie3, websocket; print(sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) { throw "Bundled Python import check failed." }
    & $portablePython -c "import wechat_autoreply, wechat_web, wechat_media, gemini_web_client, wechat_knowledge, pypdf; print('Project import OK')"
    if ($LASTEXITCODE -ne 0) { throw "Project import check failed." }
} finally {
    Pop-Location
}

$manifest = @(
    "WeChat\Weixin.exe",
    "WeChat\version.dll",
    "WeChat\4.1.9.52\Weixin.dll",
    "runtime\python\python.exe",
    "wechat_autoreply.py",
    "wechat_knowledge.py",
    "wechat_web.py",
    "web_panel.js",
    "requirements.txt",
    "start_portable.ps1"
) | ForEach-Object {
    $path = Join-Path $Target $_
    [pscustomobject]@{
        File = $_
        SHA256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    }
}
$manifest | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $Target "manifest.sha256.json") -Encoding UTF8

$bytes = (Get-ChildItem -LiteralPath $Target -Recurse -File |
    Measure-Object -Property Length -Sum).Sum
Write-Host ("Ready: {0} ({1:N2} GB)" -f $Target, ($bytes / 1GB)) -ForegroundColor Green
Write-Host "This folder contains private configuration, memory and cookies. Keep it private."
