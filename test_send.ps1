# Test WeChat-Hook text send (loopback only).
# Requires: version.dll deployed to D:\wx, WeChat restarted so the hook loads.

param(
    [string]$To  = "filehelper",       # 文件传输助手
    [string]$Msg = "hook test 你好 " + (Get-Date -Format "HH:mm:ss")
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$Url = "http://127.0.0.1:30001/SendTextMsg"

# 1) probe port
$tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port 30001 -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    Write-Host "[x] 30001 未监听：微信可能未重启，或 hook 未加载。" -ForegroundColor Red
    Write-Host "    请关闭全部微信进程后重新启动 D:\wx\Weixin.exe，再运行本脚本。" -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] 127.0.0.1:30001 已监听" -ForegroundColor Green

# 2) send
$body = @{ wxidorgid = $To; msg = $Msg } | ConvertTo-Json -Compress
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
try {
    $resp = Invoke-RestMethod -Uri $Url -Method POST -Body $bytes -ContentType "application/json; charset=utf-8"
    Write-Host "[ok] 已发送 -> $To : $Msg" -ForegroundColor Green
    Write-Host ("     响应: " + ($resp | ConvertTo-Json -Compress)) -ForegroundColor Green
}
catch {
    Write-Host "[x] 发送请求失败: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
