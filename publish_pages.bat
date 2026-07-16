@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
set LOCKFILE=%~dp0run.lock
if exist "%LOCKFILE%" (
  echo %date% %time% Another run appears active. Exiting.>> logs\publish_pages.log
  exit /b 0
)
echo %date% %time% > "%LOCKFILE%"
python monitor.py >> logs\publish_pages.log 2>&1
set MONITOR_EXIT=%errorlevel%
if not "%MONITOR_EXIT%"=="0" (
  echo %date% %time% monitor.py failed with %MONITOR_EXIT%>> logs\publish_pages.log
  del "%LOCKFILE%"
  exit /b %MONITOR_EXIT%
)
git add docs README.md MONITORING_STRATEGY.md monitor.py send_telegram.py requirements.txt run_daily.bat publish_pages.bat .gitignore .env.example >> logs\publish_pages.log 2>&1
git commit -m "Update PAGCOR report %date% %time%" >> logs\publish_pages.log 2>&1
if errorlevel 1 echo %date% %time% No git changes to commit.>> logs\publish_pages.log
git push >> logs\publish_pages.log 2>&1
set PUSH_EXIT=%errorlevel%
if not "%PUSH_EXIT%"=="0" echo %date% %time% git push failed with %PUSH_EXIT%>> logs\publish_pages.log
del "%LOCKFILE%"
exit /b %PUSH_EXIT%
