@echo off
rem Transkribator installer for Windows 10/11. Double-click to install.
rem Options are passed to scripts\install.ps1: -AllModels, -Model large-v3-turbo, -NoShortcut, -Cpu, -Ci
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set RC=%ERRORLEVEL%
if /I not "%~1"=="-Ci" pause
exit /b %RC%
