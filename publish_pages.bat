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
if "%TG_ARGS%"=="" echo %date% %time% No Telegram summary sent - both monitors failed this run.>> logs\publish_pages.log
if "%TG_ARGS%"=="" goto :after_tg

set TG_ATTEMPT=0
:tg_send
set /a TG_ATTEMPT+=1
python send_telegram.py%TG_ARGS% >> logs\publish_pages.log 2>&1
set TG_EXIT=%errorlevel%
if "%TG_EXIT%"=="0" goto :after_tg
if %TG_ATTEMPT% GEQ 3 goto :tg_failed
echo %date% %time% Telegram send failed with %TG_EXIT%, attempt %TG_ATTEMPT%/3, retrying in 30s>> logs\publish_pages.log
timeout /t 30 /nobreak >nul
goto :tg_send
:tg_failed
echo %date% %time% Combined Telegram send failed after %TG_ATTEMPT% attempts with %TG_EXIT%>> logs\publish_pages.log
:after_tg

git add docs README.md MONITORING_STRATEGY.md MEITY_MONITORING_STRATEGY.md monitor.py meity_monitor.py send_telegram.py requirements.txt run_daily.bat publish_pages.bat .gitignore .env.example >> logs\publish_pages.log 2>&1
git commit -m "Update PAGCOR + MeitY report %date% %time%" >> logs\publish_pages.log 2>&1
if errorlevel 1 echo %date% %time% No git changes to commit.>> logs\publish_pages.log

set PUSH_ATTEMPT=0
:push_retry
set /a PUSH_ATTEMPT+=1
git push >> logs\publish_pages.log 2>&1
set PUSH_EXIT=%errorlevel%
if "%PUSH_EXIT%"=="0" goto :push_done
if %PUSH_ATTEMPT% GEQ 3 goto :push_failed
echo %date% %time% git push failed with %PUSH_EXIT%, attempt %PUSH_ATTEMPT%/3, retrying in 60s>> logs\publish_pages.log
timeout /t 60 /nobreak >nul
goto :push_retry
:push_failed
echo %date% %time% git push failed after %PUSH_ATTEMPT% attempts with %PUSH_EXIT%>> logs\publish_pages.log
:push_done

del "%LOCKFILE%"
exit /b %PUSH_EXIT%
