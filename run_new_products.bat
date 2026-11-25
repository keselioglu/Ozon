@echo off
REM Daily 03:00 new products to Ozon, M&S + H&M (Task Scheduler: OzonSales_DailyNewProducts).
REM run_new_products.py takes today's numbers from daily_plan.json (Ozon Sales app, Prioritization
REM tab): finds products not on Ozon, crawls them fresh, translates, uploads new offers only,
REM waits for Ozon's import result, then sets stock for the new offers only. Plan 0 -> does nothing.
REM Writes new_products_daily_YYYY-MM-DD.log, new_products_status.json, new_products_history.jsonl.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=new_products_daily_%D%.log

echo [%date% %time%] Starting new products run >> %LOG%
%PY% -u run_new_products.py >> %LOG% 2>&1
set RC=%errorlevel%
echo [%date% %time%] Finished with exit code %RC% >> %LOG%
exit /b %RC%
