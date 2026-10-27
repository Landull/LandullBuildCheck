@echo off
rem Double-click to build dist\LandullBuildCheck.exe (one file, asks for administrator when opened).
rem First run downloads Qt and the sensor library: about 3-5 minutes.
cd /d "%~dp0"
echo == Landull BuildCheck: Windows build ==

if not exist .venv (
  for %%v in (3.13 3.12 3.11 3.10) do if not exist .venv py -%%v -m venv .venv >nul 2>&1
  if not exist .venv python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Python 3.10-3.13 is required: https://www.python.org/downloads/
  goto :fail
)
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt || goto :fail

if not exist lhm\LibreHardwareMonitorLib.dll (
  echo Downloading the LibreHardwareMonitor sensor library...
  .venv\Scripts\python fetch_lhm.py
)
set LHM=
if exist lhm\LibreHardwareMonitorLib.dll (
  set LHM=--add-data "lhm;lhm"
) else (
  echo WARNING: building without LibreHardwareMonitor - the app will only have basic sensors.
)

.venv\Scripts\pyinstaller --noconfirm --clean --log-level WARN --onefile --windowed --uac-admin ^
  --name LandullBuildCheck --icon assets\icon.ico --add-data "assets;assets" %LHM% --paths . ^
  --hidden-import bc_workers --hidden-import bc_collect --hidden-import bc_sensors --hidden-import bc_report ^
  --collect-all pythonnet --collect-all clr_loader ^
  buildcheck.py || goto :fail
rmdir /s /q build 2>nul
del LandullBuildCheck.spec 2>nul
echo.
echo BUILD OK -^> %cd%\dist\LandullBuildCheck.exe
pause
exit /b 0

:fail
echo BUILD FAILED
pause
exit /b 1
