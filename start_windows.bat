@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto run
call "%~dp0setup_windows.bat" --no-pause
if errorlevel 1 goto failure
:run
".venv\Scripts\python.exe" -m pc_subtitle_translator %*
if errorlevel 1 goto failure
exit /b 0
:failure
echo.
echo The app could not start. Review the error above.
pause
exit /b 1
