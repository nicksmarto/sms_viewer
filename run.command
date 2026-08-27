#!/bin/bash
#
# SMS Backup Viewer — one-click launcher (macOS / Linux)
#
# Double-click this file in Finder, or run ./run.command in a terminal.
# On the FIRST run it sets everything up automatically:
#   1. creates an isolated Python environment ("venv")
#   2. installs the app's dependencies into it
# On EVERY run it then starts the app, which opens your browser automatically.
# You never have to touch the terminal yourself.

# Always work from the folder this script lives in (so double-click works).
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "        SMS Backup Viewer — launcher"
echo "=============================================="

# --- Check Python is available --------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "ERROR: Python 3 is not installed (or not on your PATH)."
  echo "Install it from https://www.python.org/downloads/ and try again."
  echo
  read -r -p "Press Return to close this window..."
  exit 1
fi

# --- First-run setup: create the virtual environment ----------------------
if [ ! -d "venv" ]; then
  echo
  echo "First-time setup: creating the virtual environment (this happens once)..."
  python3 -m venv venv || {
    echo "ERROR: could not create the virtual environment."
    read -r -p "Press Return to close this window..."
    exit 1
  }
fi

# --- Activate the environment and install/update dependencies -------------
# shellcheck disable=SC1091
source venv/bin/activate
echo "Checking dependencies (fast after the first run)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt || {
  echo "ERROR: could not install dependencies (are you online?)."
  read -r -p "Press Return to close this window..."
  exit 1
}

# --- Launch the app -------------------------------------------------------
echo
echo "Starting SMS Backup Viewer..."
echo "Your browser will open at http://127.0.0.1:5000"
echo "Keep this window open while you use the app; close it to stop the app."
echo
python app.py

# If the app stops or errors, keep the window open so you can read any message.
echo
read -r -p "The app has stopped. Press Return to close this window..."
