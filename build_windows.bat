@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto install
call "%~dp0setup_windows.bat" --no-pause
if errorlevel 1 goto failure
:install
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r "requirements-dev.txt"
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name PcSubtitleTranslator --paths "." --collect-all pyaudiowpatch --collect-all soxr --hidden-import keyring.backends.Windows --hidden-import win32ctypes.pywin32.win32cred "scripts\launch.py"
if errorlevel 1 goto failure
echo.
echo Build finished: dist\PcSubtitleTranslator\PcSubtitleTranslator.exe
echo Keep the entire dist\PcSubtitleTranslator folder together.
echo Test Windows audio capture and your API connection before distributing it.
pause
exit /b 0
:failure
echo.
echo The Windows build did not finish. Review the error above.
pause
exit /b 1
