@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WeChat Local AI - Start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
pause
