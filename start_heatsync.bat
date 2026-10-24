@echo off
cd /d %~dp0

set LOGFILE=%~dp0heatsync.log

:: When relaunched straight after an update, keep the log already started
if /i not "%~1"=="updated" (
    echo HeatSync Log - %date% %time% > "%LOGFILE%"
    echo. >> "%LOGFILE%"
)

:: Check GitHub for a newer version and install it before starting.
:: The next line must stay ONE line: the update can rewrite this .bat while it
:: runs, and cmd must hand over to the new copy without reading any more of it.
if /i not "%~1"=="updated" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_heatsync.ps1" "%LOGFILE%" & if errorlevel 2 if not errorlevel 3 (call "%~f0" updated & exit /b)

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

:: An update may have changed the dependencies
if /i "%~1"=="updated" (
    echo Installing any new dependencies...
    echo [SETUP] Updating dependencies >> "%LOGFILE%"
    pip install -r requirements.txt >> "%LOGFILE%" 2>&1
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
