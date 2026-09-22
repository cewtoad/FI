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
    echo [!] 没找到 Python。
    echo     请使用"全量语音包"（内含 Python 运行时），或自行安装 Python 3.12。
    pause
    exit /b 1
)

rem Interactive launcher: choose 1) web panel or 2) voice mode.
rem (PY may be a bare path or a launcher command with args, so don't quote it.)
%PY% FI.py

echo.
echo 已退出。
timeout /t 3 /nobreak >nul
exit /b 0
