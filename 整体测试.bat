@echo off
chcp 65001 >nul
title F1 Race Engineer - Self Test
cd /d "%~dp0"

set "PY="
if exist "%~dp0python.exe" set "PY=%~dp0python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3.12"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [!] Python not found. Use the full package, or install Python 3.12.
    pause
    exit /b 1
)

:menu
cls
echo ==================================================
echo   F1 Race Engineer  -  Self Test
echo ==================================================
echo.
echo   [A] Self check (all subsystems)      ^<- start here
echo.
echo   [1] Web mode   (browser panel)
echo   [2] Voice mode (numpad + push-to-talk)
echo   [3] Console panel (live data)
echo.
echo   [4] Audio devices (mic / speaker)
echo   [5] Play test voice
echo   [6] Recent session records
echo.
echo   [7] Run unit tests (pytest)
echo   [q] Quit
echo.
set /p "c=Select: "

if /i "%c%"=="a" goto self
if /i "%c%"=="1" goto web
if /i "%c%"=="2" goto voice
if /i "%c%"=="3" goto console
if /i "%c%"=="4" goto dev
if /i "%c%"=="5" goto speak
if /i "%c%"=="6" goto sessions
if /i "%c%"=="7" goto pytest
if /i "%c%"=="q" goto end
goto menu

:self
cls
%PY% tool_selftest.py
echo.
pause
goto menu

:web
cls
echo   Web mode: browser opens http://127.0.0.1:8765
echo   Close this window (or Ctrl+C) to stop.
echo.
%PY% FI.py --web
echo.
pause
goto menu

:voice
cls
echo   Voice mode: in game press NUMPAD + to talk, again to stop. Ctrl+C to quit.
echo.
%PY% voice_main.py
echo.
pause
goto menu

:console
cls
echo   Console panel: live data (Ctrl+C to quit).
echo.
%PY% run.py
echo.
pause
goto menu

:dev
cls
%PY% tool_audio.py show
echo.
pause
goto menu

:speak
cls
%PY% tool_audio.py speak
echo.
pause
goto menu

:sessions
cls
echo   Recent sessions (sessions/):
echo.
dir /b /o-d "sessions\*.json" 2>nul
echo.
echo   Open the latest TXT report in Notepad?
set /p "o=Type y to open: "
if /i "%o%"=="y" (
    for /f "delims=" %%f in ('dir /b /o-d "sessions\*.txt" 2^>nul') do (
        start "" "%%f"
        goto menu
    )
    echo   No TXT report found.
)
echo.
pause
goto menu

:pytest
cls
echo   Running unit tests...
echo.
%PY% -m pytest -q
echo.
pause
goto menu

:end
exit /b 0
