@echo off
rem Resets an account password on this computer. Keep this file next to app.py.
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY (
  echo Python 3.9 or newer is required.
  pause
  exit /b 1
)
%PY% reset_password.py
pause
