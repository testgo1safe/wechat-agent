# Local Mini LLM chat (Ollama + Qwen2.5 1.5B)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$Model = "qwen2.5:1.5b"
$ApiUrl = "http://localhost:11434/api/generate"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Local AI (Qwen2.5 1.5B)" -ForegroundColor Cyan
Write-Host "  Type: exit / quit" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

while ($true) {
    $userInput = Read-Host "You"
    if ($userInput -in @("exit", "quit")) {
        Write-Host "Bye!" -ForegroundColor Yellow
        break
    }
    if ([string]::IsNullOrWhiteSpace($userInput)) { continue }

    $body = @{
        model  = $Model
        prompt = $userInput
        stream = $false
    } | ConvertTo-Json -Compress

    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        $resp = Invoke-RestMethod -Uri $ApiUrl -Method POST -Body $bytes -ContentType "application/json; charset=utf-8"
        Write-Host ""
        Write-Host "AI: $($resp.response)" -ForegroundColor Green
        Write-Host ""
    }
    catch {
        Write-Host "Error: Ollama is not running. Check system tray icon." -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red
        break
    }
}