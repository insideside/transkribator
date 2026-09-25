@echo off
rem Starts Transkribator: local server + browser. Close this window to stop.
title Transkribator
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Transkribator is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
