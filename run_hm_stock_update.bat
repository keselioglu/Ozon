@echo off
REM Daily 02:00 H&M stock refresh (Task Scheduler: OzonSales_DailyHMStockUpdate).
REM refresh_hm_stock.py opens a normal Chrome window (own profile), reads H&M TR stock,
REM pushes 5 / 1 / 0 to Ozon. Aborts without pushing if >20% of products can't be read.
cd /d "%~dp0"
set PYTHONUTF8=1
set PY="C:\Users\Tugberk\AppData\Local\Programs\Python\Python314\python.exe"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%d
set LOG=hm_stock_daily_%D%.log

echo [%date% %time%] Starting H&M stock refresh >> %LOG%
%PY% -u refresh_hm_stock.py --apply >> %LOG% 2>&1
echo [%date% %time%] Finished with exit code %errorlevel% >> %LOG%
