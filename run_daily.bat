@echo off
setlocal
cd /d "%~dp0"
python monitor.py
if errorlevel 1 exit /b %errorlevel%
python send_telegram.py
endlocal
