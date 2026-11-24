@echo off
REM Daily 10:15 - if the daily plan isn't met and quota remains, find and upload more (fill_remaining_quota.py)
REM Task Scheduler: MandsOzonQuotaFill. Fixed 2026-11-24: used the old PC's Python (C:\Users\tugbe\...), never ran on this PC.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=quota_fill_%D%.log
echo [%date% %time%] Starting fill_remaining_quota.py >> %LOG%
%PY% -u fill_remaining_quota.py >> %LOG% 2>&1
set RC=%errorlevel%
echo [%date% %time%] Finished with exit code %RC% >> %LOG%
exit /b %RC%