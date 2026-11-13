@echo off
rem Starts the tool with Python. Keep this file next to app.py.
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY (
  echo Python 3.9 or newer is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)
%PY% start_lan.py
if errorlevel 1 pause
