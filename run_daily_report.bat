@echo off
REM Daily 10:00 - business report + Telegram summary (daily_report.py), then the
REM Ozon Sales app's Overview refresh: catalog_status.py (read-only: live ids,
REM stock, prices) and account_snapshot.py (offline). Task #95.
REM Task Scheduler: MandsOzonDailyReport. Fixed 2026-11-24: used the old PC's Python (C:\Users\tugbe\...), never ran on this PC.
REM Exit code is daily_report.py's; the two refresh steps are only logged.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=daily_report_%D%.log
echo [%date% %time%] Starting daily_report.py >> %LOG%
%PY% -u daily_report.py >> %LOG% 2>&1
set RC=%errorlevel%
echo [%date% %time%] Finished with exit code %RC% >> %LOG%
echo [%date% %time%] Starting catalog_status.py >> %LOG%
%PY% -u catalog_status.py >> %LOG% 2>&1
set RC2=%errorlevel%
echo [%date% %time%] catalog_status.py finished with exit code %RC2% >> %LOG%
echo [%date% %time%] Starting account_snapshot.py >> %LOG%
%PY% -u account_snapshot.py >> %LOG% 2>&1
set RC3=%errorlevel%
echo [%date% %time%] account_snapshot.py finished with exit code %RC3% >> %LOG%
exit /b %RC%
