@echo off
title galaxypron - Setup
echo.
echo  ============================================
echo     galaxypron - Edge AI Agent
echo     One-time setup
echo  ============================================
echo.
cd /d "%~dp0"

REM Use the installed Python; fall back to PATH python
if exist "C:\Users\User\Python312\python.exe" (
    set "PYTHON=C:\Users\User\Python312\python.exe"
) else (
    set "PYTHON=python"
)

echo  Python: %PYTHON%
"%PYTHON%" --version

echo.
echo  Step 1: Creating Python virtual environment...
if not exist venv (
    "%PYTHON%" -m venv venv
)

echo  Step 2: Installing dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo  Setup complete!
echo  Starting galaxypron...
call start_galaxypron.bat
