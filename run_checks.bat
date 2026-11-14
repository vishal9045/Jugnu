@echo off
rem Runs every module's own checks. No keys, microphone or internet needed; each prints ok.
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (echo Run install.bat first. & pause & exit /b 1)
set "FAILED="
for %%M in (guard pronounce speak ears whose face argon brain auditors) do (
  echo == %%M
  "%PY%" %%M.py || set "FAILED=1"
)
echo == setup
"%PY%" setup_jugnu.py test || set "FAILED=1"
echo == jugnu
"%PY%" -c "import jugnu; jugnu._selfcheck(); print('ok')" || set "FAILED=1"
echo.
if defined FAILED (echo Something failed: see above.) else (echo All checks passed.)
pause
