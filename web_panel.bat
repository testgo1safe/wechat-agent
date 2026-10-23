@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 启动管理面板 http://127.0.0.1:8848  (关闭此窗口即停止)
python -u wechat_web.py --port 8848
pause
