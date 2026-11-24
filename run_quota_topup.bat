@echo off
REM Daily 05:00 - quota top-up: second upload_to_ozon.py pass over deferred_items.json (same daily plan cap)
REM Task Scheduler: MandsOzonQuotaTopup. Fixed 2026-11-24: used the old PC's Python (C:\Users\tugbe\...), never ran on this PC.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=quota_topup_%D%.log
echo [%date% %time%] Starting upload_to_ozon.py >> %LOG%
%PY% -u upload_to_ozon.py >> %LOG% 2>&1
set RC=%errorlevel%
echo [%date% %time%] Finished with exit code %RC% >> %LOG%
exit /b %RC%