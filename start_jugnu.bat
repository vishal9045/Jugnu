@echo off
rem Starts Jugnu in the system tray (the "J" icon by the clock). Running it twice is harmless.
rem The "Jugnu" shortcut next to this file (and in the Start menu) runs it with its icon; setup adds one at login.
rem For the old console version (handy when debugging): .venv\Scripts\python jugnu.py
cd /d "%~dp0"
if not exist .env (start "" ".venv\Scripts\pythonw.exe" setup_jugnu.py & exit /b)
start "Jugnu" /min ".venv\Scripts\python.exe" tray.py
