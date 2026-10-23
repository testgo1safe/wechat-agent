@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autoreply_service.ps1" stop
timeout /t 2 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autoreply_service.ps1" start
timeout /t 2 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%~dp0autoreply.log') { Get-Content '%~dp0autoreply.log' -Tail 10 -Encoding UTF8 }"
pause
