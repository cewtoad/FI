@echo off
chcp 65001 >nul
title F1 Race Engineer
cd /d "%~dp0"

rem --- Pick a Python: embedded runtime first, then py launcher, then python ---
set "PY="
if exist "%~dp0python.exe" set "PY=%~dp0python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3.12"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [!] Python not found. Use the full pack or install Python 3.12.
    pause
    exit /b 1
)

rem Interactive launcher: choose 1) web panel or 2) voice mode.
rem Quoted embedded runtime: a pack path with spaces would break the launch
rem (%PY% stays unquoted for the bare "py -3.12"/"python" launchers).
if exist "%~dp0python.exe" (
    "%~dp0python.exe" FI.py
) else (
    %PY% FI.py
)
if errorlevel 1 pause

echo.
echo Exited.
timeout /t 3 /nobreak >nul
exit /b 0
