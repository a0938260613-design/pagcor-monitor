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
if not "%MONITOR_EXIT%"=="0" echo %date% %time% monitor.py failed with %MONITOR_EXIT% - continuing so MeitY still gets a chance to publish.>> logs\publish_pages.log

python meity_monitor.py >> logs\publish_pages.log 2>&1
set MEITY_EXIT=%errorlevel%
if not "%MEITY_EXIT%"=="0" echo %date% %time% meity_monitor.py failed with %MEITY_EXIT% - continuing so PAGCOR's own results still get pushed.>> logs\publish_pages.log

rem Combine whichever monitor(s) actually succeeded this run into ONE Telegram
rem message - never include a stale leftover summary from a previous run for
rem the side that just failed.
set TG_ARGS=
if "%MONITOR_EXIT%"=="0" set TG_ARGS=%TG_ARGS% reports\telegram_summary.txt
if "%MEITY_EXIT%"=="0" set TG_ARGS=%TG_ARGS% reports\meity_telegram_summary.txt
if not "%TG_ARGS%"=="" python send_telegram.py%TG_ARGS% >> logs\publish_pages.log 2>&1
if not "%TG_ARGS%"=="" set TG_EXIT=%errorlevel%
if "%TG_ARGS%"=="" echo %date% %time% No Telegram summary sent - both monitors failed this run.>> logs\publish_pages.log
if defined TG_EXIT if not "%TG_EXIT%"=="0" echo %date% %time% Combined Telegram send failed with %TG_EXIT%>> logs\publish_pages.log

git add docs README.md MONITORING_STRATEGY.md MEITY_MONITORING_STRATEGY.md monitor.py meity_monitor.py send_telegram.py requirements.txt run_daily.bat publish_pages.bat .gitignore .env.example >> logs\publish_pages.log 2>&1
git commit -m "Update PAGCOR + MeitY report %date% %time%" >> logs\publish_pages.log 2>&1
if errorlevel 1 echo %date% %time% No git changes to commit.>> logs\publish_pages.log
git push >> logs\publish_pages.log 2>&1
set PUSH_EXIT=%errorlevel%
if not "%PUSH_EXIT%"=="0" echo %date% %time% git push failed with %PUSH_EXIT%>> logs\publish_pages.log
del "%LOCKFILE%"
exit /b %PUSH_EXIT%
