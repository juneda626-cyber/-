@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python launcher not found. Install Python first.
  pause
  exit /b 1
)
py -3 -c "import pypdf" >nul 2>&1
if errorlevel 1 (
  py -3 -m pip install -r requirements.txt
  if errorlevel 1 (
    echo PDF library installation failed. Copy the error message.
    pause
    exit /b 1
  )
)
echo Starting FLUXIA. Keep this window open. Press Ctrl+C to stop.
py -3 -c "import threading,webbrowser,runpy; t=threading.Timer(2,lambda:webbrowser.open('http://127.0.0.1:4173')); t.daemon=True; t.start(); runpy.run_path('server.py',run_name='__main__')"
pause
