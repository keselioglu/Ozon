@echo off
REM Launcher for Windows Task Scheduler — 5am quota top-up step (business
REM instruction, 2026-08-31): checks remaining daily_create quota and pushes
REM more of the deferred queue if any exists. upload_to_ozon.py already
REM handles this (resumes deferred_items.json, respects the quota) with no
REM changes needed — this just gives it a second daily invocation.

cd /d "%~dp0"
"C:\Users\tugbe\AppData\Local\Python\pythoncore-3.14-64\python.exe" upload_to_ozon.py
