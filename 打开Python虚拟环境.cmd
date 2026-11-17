@echo off
chcp 65001 >nul
title Smart CS - Python virtual environment
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" goto missing
call ".venv\Scripts\activate.bat"
set PYTHONUTF8=1
echo.
echo Activated project Python environment:
python -c "import sys; print(sys.executable); print('Virtual environment:', sys.prefix != sys.base_prefix)"
echo.
echo Run: python start.py --open-browser
"%ComSpec%" /K
exit /b 0
:missing
echo Project virtual environment not found.
pause
exit /b 1
