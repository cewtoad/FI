@echo off
chcp 65001 >nul
title F1 Race Engineer
cd /d "%~dp0"

echo ============================================
echo   F1 Race Engineer
echo ============================================
echo.
echo   Starting telemetry receiver + web UI...
echo   Browser will open automatically.
echo.
echo   Keep this window open while racing.
echo   Close this window (or press Ctrl+C) to stop.
echo.

rem Pick a Python: prefer the py launcher, fall back to python.
where py >nul 2>nul
if %errorlevel%==0 (
    set PY=py -3.12
) else (
    set PY=python
)

rem Start the web server in a new window so this one can wait then open the browser.
start "F1TR-server" cmd /c "%PY% run.py --web"

rem Wait for the server to bind before opening the browser.
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765"

echo.
echo   Web UI: http://127.0.0.1:8765
echo   If the page does not load, wait a moment and refresh.
echo.
echo   Press any key to stop the server and exit.
pause >nul

rem Kill the server window and its process tree only (by window title).
taskkill /FI "WINDOWTITLE eq F1TR-server*" /T /F >nul 2>nul
echo Stopped.
timeout /t 2 /nobreak >nul
exit /b 0
