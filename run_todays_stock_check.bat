@echo off
REM Daily 06:00 - stock push for today's new offer_ids that cleared moderation after 4am
REM Task Scheduler: MandsOzonTodaysStockCheck. Fixed 2026-11-24: used the old PC's Python (C:\Users\tugbe\...), never ran on this PC.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=todays_stock_check_%D%.log
echo [%date% %time%] Starting check_todays_stock.py >> %LOG%
%PY% -u check_todays_stock.py >> %LOG% 2>&1
set RC=%errorlevel%
echo [%date% %time%] Finished with exit code %RC% >> %LOG%
exit /b %RC%