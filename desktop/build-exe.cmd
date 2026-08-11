@echo off
setlocal EnableDelayedExpansion
set "DESKTOP_DIR=%~dp0"
set "VENV=%DESKTOP_DIR%.venv-build"
set "PYTHON=%VENV%\Scripts\python.exe"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
set "ASSEMBLY_INFO=%DESKTOP_DIR%build-launcher\AssemblyInfo.cs"
set "APP_ICON_ICO=%DESKTOP_DIR%assets\codex-meter-icon.ico"
set "APP_ICON_PNG=%DESKTOP_DIR%assets\codex-meter-icon.png"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"

if not exist "%PYTHON%" (
  set "BASE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if not exist "!BASE_PYTHON!" set "BASE_PYTHON=python"
  "!BASE_PYTHON!" -m venv "%VENV%" || exit /b 2
)

"%PYTHON%" -m pip install -r "%DESKTOP_DIR%requirements.txt" -r "%DESKTOP_DIR%requirements-build.txt" || exit /b 3
"%PYTHON%" "%DESKTOP_DIR%make_launcher_metadata.py" || exit /b 9
if not exist "%CSC%" (
  echo ERROR: .NET Framework C# compiler was not found.
  exit /b 8
)
if not exist "%APP_ICON_ICO%" exit /b 10
if not exist "%APP_ICON_PNG%" exit /b 11

"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --console ^
  --name CodexMeterSync.Worker ^
  --icon "%APP_ICON_ICO%" ^
  --add-data "%APP_ICON_PNG%;assets" ^
  --collect-all pystray ^
  --collect-all watchfiles ^
  --collect-submodules bleak.backends.winrt ^
  --distpath "%DESKTOP_DIR%dist-worker" ^
  --workpath "%DESKTOP_DIR%build-worker" ^
  --specpath "%DESKTOP_DIR%build-worker" ^
  "%DESKTOP_DIR%tray_app.py" || exit /b 4
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu ^
  /win32icon:"%APP_ICON_ICO%" ^
  /out:"%DESKTOP_DIR%..\CodexMeterSync.exe" ^
  /resource:"%DESKTOP_DIR%dist-worker\CodexMeterSync.Worker.exe",WorkerPayload ^
  "%DESKTOP_DIR%hidden_launcher.cs" "%ASSEMBLY_INFO%" || exit /b 6

"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --console ^
  --name CodexMeterSync-Test.Worker ^
  --icon "%APP_ICON_ICO%" ^
  --add-data "%APP_ICON_PNG%;assets" ^
  --collect-all pystray ^
  --collect-all watchfiles ^
  --collect-submodules bleak.backends.winrt ^
  --distpath "%DESKTOP_DIR%dist-test-worker" ^
  --workpath "%DESKTOP_DIR%build-test-worker" ^
  --specpath "%DESKTOP_DIR%build-test-worker" ^
  "%DESKTOP_DIR%tray_app_test.py" || exit /b 5
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu ^
  /win32icon:"%APP_ICON_ICO%" ^
  /out:"%DESKTOP_DIR%..\CodexMeterSync-Test.exe" ^
  /resource:"%DESKTOP_DIR%dist-test-worker\CodexMeterSync-Test.Worker.exe",WorkerPayload ^
  "%DESKTOP_DIR%hidden_launcher.cs" "%ASSEMBLY_INFO%" || exit /b 7
echo Built %DESKTOP_DIR%..\CodexMeterSync.exe
echo Built %DESKTOP_DIR%..\CodexMeterSync-Test.exe
