@echo off
setlocal EnableDelayedExpansion
set "PYTHONUTF8=1"
set "DESKTOP_DIR=%~dp0"
set "VENV=%DESKTOP_DIR%.venv"
set "VENV_PYTHON=%VENV%\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
  set "BASE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if not exist "%BASE_PYTHON%" (
    where py >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=py -3"
  )
  if not defined BASE_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=python"
  )
  if not defined BASE_PYTHON (
    echo ERROR: Python 3 was not found.
    exit /b 2
  )

  echo Creating local Python environment...
  !BASE_PYTHON! -m venv "%VENV%"
  if errorlevel 1 exit /b 3
)

"%VENV_PYTHON%" -c "import bleak, watchfiles" >nul 2>nul
if errorlevel 1 (
  echo Installing Bluetooth dependency...
  "%VENV_PYTHON%" -m pip install -r "%DESKTOP_DIR%requirements.txt"
  if errorlevel 1 exit /b 4
)

echo Starting Codex BLE sync. Press Ctrl+C to stop.
"%VENV_PYTHON%" "%DESKTOP_DIR%sync.py" %*
