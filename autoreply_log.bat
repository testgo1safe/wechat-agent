@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "LOG=autoreply.log"
if not exist "%LOG%" set "LOG=autoreply_run.log"
if not exist "%LOG%" (
  echo No log file yet.
  pause
  exit /b 1
)
python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); p=r'%LOG%'; lines=open(p,encoding='utf-8-sig',errors='replace').read().splitlines(); print('\n'.join(lines[-40:]))"
echo.
echo (log file: %LOG%  encoding: UTF-8)
pause
