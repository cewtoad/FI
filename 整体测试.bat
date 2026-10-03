@echo off
chcp 65001 >nul
title F1 Race Engineer - Self Test
cd /d "%~dp0"

rem NOTE: the embedded full path is NEVER stored in PY and never expanded
rem inside a block - a pack path like "D:older (3)\" would inject an
rem unquoted ")" at block parse time and kill the script. PY only ever holds
rem a bare launcher name ("py -3.12" / "python"); the embedded runtime is
rem launched as the quoted "%~dp0python.exe" at each call site.
set "EMBEDDED="
if exist "%~dp0python.exe" set "EMBEDDED=1"
where py >nul 2>nul && set "PY=py -3.12"
if not defined PY if not defined EMBEDDED (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY if not defined EMBEDDED (
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
echo   [7] Run unit tests (pytest, offline)
echo   [8] Run ALL tests (incl. network/audio)
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
if /i "%c%"=="8" goto pytestall
if /i "%c%"=="q" goto end
goto menu

:self
cls
if defined EMBEDDED ( "%~dp0python.exe" tool_selftest.py 
) else ( %PY% tool_selftest.py )
echo.
pause
goto menu

:web
cls
echo   Web mode: browser opens http://127.0.0.1:8765
echo   Close this window (or Ctrl+C) to stop.
echo.
if defined EMBEDDED ( "%~dp0python.exe" FI.py --web 
) else ( %PY% FI.py --web )
echo.
pause
goto menu

:voice
cls
echo   Voice mode: in game press NUMPAD + to talk, again to stop. Ctrl+C to quit.
echo.
if defined EMBEDDED ( "%~dp0python.exe" voice_main.py 
) else ( %PY% voice_main.py )
echo.
pause
goto menu

:console
cls
echo   Console panel: live data (Ctrl+C to quit).
echo.
if defined EMBEDDED ( "%~dp0python.exe" run.py 
) else ( %PY% run.py )
echo.
pause
goto menu

:dev
cls
if defined EMBEDDED ( "%~dp0python.exe" tool_audio.py show 
) else ( %PY% tool_audio.py show )
echo.
pause
goto menu

:speak
cls
if defined EMBEDDED ( "%~dp0python.exe" tool_audio.py speak 
) else ( %PY% tool_audio.py speak )
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
echo   Running unit tests (offline)...
echo.
if defined EMBEDDED ( "%~dp0python.exe" -m pytest -q 
) else ( %PY% -m pytest -q )
echo.
pause
goto menu

:pytestall
cls
echo   Running ALL tests (loopback UDP + audio)...
echo.
set "RUN_NETWORK_TESTS=1"
if defined EMBEDDED ( "%~dp0python.exe" -m pytest -q 
) else ( %PY% -m pytest -q )
set "RUN_NETWORK_TESTS="
echo.
pause
goto menu

:end
exit /b 0
