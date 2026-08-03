@echo off
setlocal
set "PYTHONUTF8=1"

if "%~1"=="" (
  echo Usage: flash-firmware.cmd COM8
  echo.
  echo Available serial ports:
  reg query "HKLM\HARDWARE\DEVICEMAP\SERIALCOMM"
  exit /b 2
)

set "PORT=%~1"
set "FIRMWARE=%~dp0codex-token-monitor.factory.bin"
set "PIO_PYTHON=%USERPROFILE%\Documents\Codex\pio\penv\Scripts\python.exe"
set "PIO_ESPTOOL=%USERPROFILE%\Documents\Codex\pio\packages\tool-esptoolpy\esptool.py"

if not exist "%FIRMWARE%" (
  echo ERROR: Firmware image not found: %FIRMWARE%
  exit /b 3
)

echo Flashing Codex Token Monitor to %PORT%...
if exist "%PIO_PYTHON%" if exist "%PIO_ESPTOOL%" (
  "%PIO_PYTHON%" "%PIO_ESPTOOL%" --chip esp32s3 --port %PORT% --baud 460800 write-flash 0x0 "%FIRMWARE%"
  goto :result
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m esptool --chip esp32s3 --port %PORT% --baud 460800 write-flash 0x0 "%FIRMWARE%"
  goto :result
)

where python >nul 2>nul
if not errorlevel 1 (
  python -m esptool --chip esp32s3 --port %PORT% --baud 460800 write-flash 0x0 "%FIRMWARE%"
  goto :result
)

echo ERROR: No compatible Python/esptool installation was found.
echo Install Python 3 and esptool, or keep the project in its original Codex output folder.
exit /b 4

:result
if errorlevel 1 (
  echo.
  echo Flashing failed. Hold BOOT, reconnect USB, release BOOT, and run this command again.
  exit /b 1
)

echo.
echo Flash complete. The device will restart and advertise as Codex Meter-XXXX.
exit /b 0
