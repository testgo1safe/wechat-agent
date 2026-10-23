@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WeChat Local AI - 完整停止
echo.
echo 正在检查并结束所有相关程序（自动回复 / 面板 / 启动器）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1"
set "EC=%ERRORLEVEL%"
echo.
if "%EC%"=="0" (
  echo [完成] 已全部停止。
) else (
  echo [注意] 仍可能有残留，请查看上方列表或任务管理器中的 python.exe。
)
echo.
pause
exit /b %EC%
