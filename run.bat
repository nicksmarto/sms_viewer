@echo off
REM ==========================================================
REM  SMS Backup Viewer - one-click launcher (Windows)
REM
REM  Double-click this file in File Explorer.
REM  On the FIRST run it creates an isolated Python environment
REM  and installs dependencies; on every run it starts the app,
REM  which opens your browser automatically.
REM ==========================================================

REM Work from the folder this script lives in.
cd /d "%~dp0"

echo ==============================================
echo         SMS Backup Viewer - launcher
echo ==============================================

REM --- Check Python is available ---
where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Python is not installed or not on your PATH.
  echo Install it from https://www.python.org/downloads/
  echo ^(tick "Add Python to PATH" during install^) and try again.
  echo.
  pause
  exit /b 1
)

REM --- First-run setup: create the virtual environment ---
if not exist "venv\" (
  echo.
  echo First-time setup: creating the virtual environment ^(this happens once^)...
  python -m venv venv || (
    echo ERROR: could not create the virtual environment.
    pause
    exit /b 1
  )
)

REM --- Activate and install/update dependencies ---
call venv\Scripts\activate.bat
echo Checking dependencies ^(fast after the first run^)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || (
  echo ERROR: could not install dependencies ^(are you online?^).
  pause
  exit /b 1
)

REM --- Launch the app ---
echo.
echo Starting SMS Backup Viewer...
echo Your browser will open at http://127.0.0.1:5000
echo Keep this window open while you use the app; close it to stop the app.
echo.
python app.py

echo.
echo The app has stopped.
pause
