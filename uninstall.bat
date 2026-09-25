@echo off
rem Removes Transkribator shortcuts. Delete the folder afterwards to remove everything.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1"
pause
