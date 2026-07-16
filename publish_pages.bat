@echo off
setlocal
cd /d "%~dp0"
python monitor.py
if errorlevel 1 exit /b %errorlevel%
git add docs README.md MONITORING_STRATEGY.md monitor.py send_telegram.py requirements.txt run_daily.bat publish_pages.bat .gitignore .env.example
for /f "tokens=1-4 delims=/.: " %%a in ("%date% %time%") do set stamp=%%a-%%b-%%c_%%d
git commit -m "Update PAGCOR report %date% %time%"
if errorlevel 1 echo No git changes to commit.
git push
endlocal
