@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
python --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=python"

if not defined PYTHON_CMD (
  py -3 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  echo Python was not found. Install Python 3.11 or newer from https://python.org/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" %PYTHON_CMD% -m venv .venv
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Setup complete. Double-click run.bat to start the monitor.
pause
exit /b 0

:error
echo.
echo Setup failed. Review the message above.
pause
exit /b 1
