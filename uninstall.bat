@echo off
rem Uninstalls Jugnu: stops it, removes what was installed, and asks before deleting anything of yours.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
pause
