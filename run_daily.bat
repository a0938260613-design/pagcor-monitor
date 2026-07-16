@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
set LOCKFILE=%~dp0run.lock
if exist "%LOCKFILE%" (
  echo %date% %time% Another run appears active. Exiting.>> logs\run_daily.log
  exit /b 0
)
echo %date% %time% > "%LOCKFILE%"
python monitor.py >> logs\run_daily.log 2>&1
set MONITOR_EXIT=%errorlevel%
if not "%MONITOR_EXIT%"=="0" (
  echo %date% %time% monitor.py failed with %MONITOR_EXIT%>> logs\run_daily.log
  del "%LOCKFILE%"
  exit /b %MONITOR_EXIT%
)
python send_telegram.py >> logs\run_daily.log 2>&1
set TG_EXIT=%errorlevel%
if not "%TG_EXIT%"=="0" echo %date% %time% Telegram failed with %TG_EXIT%>> logs\run_daily.log
del "%LOCKFILE%"
exit /b %TG_EXIT%
