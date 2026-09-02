@echo off
REM Launcher for Windows Task Scheduler — runs the 10am business report.
REM Task Scheduler runs with no shell profile, so this sets cwd and calls the
REM interpreter by full path explicitly rather than relying on PATH.

cd /d "%~dp0"
"C:\Users\tugbe\AppData\Local\Python\pythoncore-3.14-64\python.exe" daily_report.py
