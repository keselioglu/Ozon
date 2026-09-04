@echo off
REM Launcher for Windows Task Scheduler — 10:15am quota-fill step (business
REM instruction, 2026-09-04): if the 10am report shows remaining daily_create
REM quota, discover and add that many new products before the day is over.

cd /d "%~dp0"
"C:\Users\tugbe\AppData\Local\Python\pythoncore-3.14-64\python.exe" fill_remaining_quota.py
