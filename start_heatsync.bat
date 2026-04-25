@echo off
cd /d %~dp0

set LOGFILE=%~dp0heatsync.log

echo HeatSync Log - %date% %time% > "%LOGFILE%"
echo. >> "%LOGFILE%"

:: Only create venv and install dependencies on first run
if not exist venv (
    echo First run - setting up HeatSync...
    echo [SETUP] Creating virtual environment >> "%LOGFILE%"
    python -m venv venv >> "%LOGFILE%" 2>&1
    call venv\Scripts\activate
    echo [SETUP] Installing dependencies >> "%LOGFILE%"
    pip install -r requirements.txt >> "%LOGFILE%" 2>&1
    echo [SETUP] Setup complete >> "%LOGFILE%"
    echo Setup complete!
) else (
    call venv\Scripts\activate
)

echo.
echo Starting HeatSync at http://localhost:5000
echo Press Ctrl+C to stop.
echo.

echo [APP] Starting app >> "%LOGFILE%"
python app.py >> "%LOGFILE%" 2>&1

echo. >> "%LOGFILE%"
echo [APP] Exited at %time% >> "%LOGFILE%"

echo.
echo HeatSync stopped. Log saved to heatsync.log
pause
