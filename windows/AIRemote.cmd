@echo off
setlocal
where py.exe >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.11 or newer from python.org, including Python Launcher and Tcl/Tk.
  pause
  exit /b 1
)
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 (
  echo AIRemote needs Python 3.11 or newer.
  pause
  exit /b 1
)
py -3 "%~dp0windows\app.py"
if errorlevel 1 pause
