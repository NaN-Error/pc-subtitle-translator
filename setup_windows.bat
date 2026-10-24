@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "PYTHONUTF8=1"

echo Setting up PC subtitle translator in this folder.
echo Requires an existing 64-bit Python 3.12, 3.13, or 3.14 installation.
echo.
if exist ".venv\Scripts\python.exe" goto install

py -3.14 -c "import sys,sysconfig; assert sysconfig.get_platform() == 'win-amd64' and (3,12) <= sys.version_info[:2] < (3,15)" >nul 2>&1
if not errorlevel 1 goto create314
py -3.13 -c "import sys,sysconfig; assert sysconfig.get_platform() == 'win-amd64' and (3,12) <= sys.version_info[:2] < (3,15)" >nul 2>&1
if not errorlevel 1 goto create313
py -3.12 -c "import sys,sysconfig; assert sysconfig.get_platform() == 'win-amd64' and (3,12) <= sys.version_info[:2] < (3,15)" >nul 2>&1
if not errorlevel 1 goto create312
python -c "import sys,sysconfig; assert sysconfig.get_platform() == 'win-amd64' and (3,12) <= sys.version_info[:2] < (3,15)" >nul 2>&1
if not errorlevel 1 goto createpython
echo No compatible Python installation was found.
echo Install 64-bit Python 3.14 from https://www.python.org/downloads/windows/
echo Then run this file again. The portable download includes its own Python.
goto failure

:create314
py -3.14 -m venv ".venv"
goto created
:create313
py -3.13 -m venv ".venv"
goto created
:create312
py -3.12 -m venv ".venv"
goto created
:createpython
python -m venv ".venv"
:created
if errorlevel 1 goto failure

:install
".venv\Scripts\python.exe" -c "import sys,sysconfig; assert sysconfig.get_platform() == 'win-amd64' and (3,12) <= sys.version_info[:2] < (3,15)" >nul 2>&1
if errorlevel 1 goto invalidenv
".venv\Scripts\python.exe" -m pip install --upgrade "pip==26.2.2"
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "requirements.txt"
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failure
echo.
echo Setup finished. Double-click start_windows.bat to open the app.
if /i not "%~1"=="--no-pause" pause
exit /b 0

:invalidenv
echo The existing .venv uses an incompatible Python version or architecture.
echo Rename or remove this project's .venv folder, then run setup again.
:failure
echo.
echo Setup did not finish. Review the error above.
if /i not "%~1"=="--no-pause" pause
exit /b 1
