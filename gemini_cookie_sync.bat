@echo off
cd /d "%~dp0"
echo Gemini Cookie request-based auto-refresh (every 10 min).
echo Debug Chrome is only needed after the Google login session fully expires.
echo Press Ctrl+C to stop.
python gemini_cookie_sync.py --watch 600
