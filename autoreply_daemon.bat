@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "PY="
if exist "%~dp0runtime\python\python.exe" set "PY=%~dp0runtime\python\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY set "PY=python"

set "SCRIPT=%~dp0wechat_autoreply.py"
set "CFG=%~dp0autoreply_config.json"
set "ERR=%~dp0autoreply_stderr.log"
set "STOP=%~dp0autoreply.stop"

:supervise
if exist "%STOP%" exit /b 0
"%PY%" -u "%SCRIPT%" --config "%CFG%" 2>>"%ERR%"
set "EXIT_CODE=%ERRORLEVEL%"
if exist "%STOP%" exit /b 0
>>"%ERR%" echo [%date% %time%] autoreply exited code=%EXIT_CODE%, restarting in 5s
timeout /t 5 /nobreak >nul
goto supervise
