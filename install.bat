@echo off
rem Installs what Jugnu needs (Python 3.12 and its libraries, in .venv here), then opens setup.
rem Safe to run again: it only adds whats missing, then opens setup to change your choices.
cd /d "%~dp0"
where uv >nul 2>nul || (
  echo Installing uv, which installs Python for Jugnu...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
if not exist .venv uv venv --python 3.12 || goto :failed
uv pip install --python .venv -r requirements.txt || goto :failed
".venv\Scripts\python.exe" setup_jugnu.py shortcuts
start "" ".venv\Scripts\pythonw.exe" setup_jugnu.py
exit /b 0
:failed
echo.
echo The install stopped with an error (above). Check your internet connection and run install.bat again.
pause
