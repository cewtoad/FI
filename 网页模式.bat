@echo off
chcp 65001 >nul
title F1 Race Engineer - Web
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
    echo [!] Python not found. Use the full pack or install Python 3.12.
    pause
    exit /b 1
)

echo Starting WEB mode... (browser will open http://127.0.0.1:8765)
echo NOTE: run ONLY one mode at a time (web OR voice), or they fight over UDP.
echo.
%PY% FI.py --web

echo.
echo Exited.
timeout /t 3 /nobreak >nul
exit /b 0
