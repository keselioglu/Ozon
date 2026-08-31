@echo off
REM Launcher for Windows Task Scheduler — 6am stock-check step (business
REM instruction, 2026-08-31): checks whether today's newly created products
REM have real stock live yet, and pushes it if not.

cd /d "%~dp0"
"C:\Users\tugbe\AppData\Local\Python\pythoncore-3.14-64\python.exe" check_todays_stock.py
