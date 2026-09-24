@echo off
rem ASCII-named launcher for the full pack (the Chinese-named .bat can mojibake
rem after some unzip tools). Same behaviour: embedded Python first.
chcp 65001 >nul
title F1 Race Engineer
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
    echo [!] Python not found.
    echo     Use the full pack (bundled runtime) or install Python 3.12.
    pause
    exit /b 1
)

%PY% FI.py

echo.
echo Exited.
timeout /t 3 /nobreak >nul
exit /b 0
