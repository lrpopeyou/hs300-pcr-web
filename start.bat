@echo off
setlocal
cd /d %~dp0
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Please install Python 3.10+ and enable the py launcher.
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
if not exist logs mkdir logs
if not exist data mkdir data
if not exist output mkdir output
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python hs300_pcr_strategy.py run --full-backfill --etf-start 20240501

REM Launch a single hidden daily updater, unless one is already running.
set PYEXE=%CD%\.venv\Scripts\python.exe
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*hs300_pcr_strategy.py daemon*' }; if (-not $p) { Start-Process -FilePath '%PYEXE%' -ArgumentList 'hs300_pcr_strategy.py daemon --etf-start 20240501 --time 15:20 --poll-seconds 60' -WorkingDirectory '%CD%' -RedirectStandardOutput '%CD%\logs\daemon.log' -RedirectStandardError '%CD%\logs\daemon_error.log' -WindowStyle Hidden }"

streamlit run app.py --server.address 127.0.0.1 --server.port 8501
pause
