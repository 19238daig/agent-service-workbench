@echo off
setlocal
chcp 65001 >nul
title Smart CS - Python local server
cd /d "%~dp0"
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" goto missing
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
".venv\Scripts\python.exe" start.py --open-browser
if errorlevel 1 goto failed
exit /b 0
:missing
echo Python virtual environment was not found:
echo %~dp0.venv
goto failed
:failed
echo.
echo Startup failed. Please keep this window open to view the error above.
pause
exit /b 1
