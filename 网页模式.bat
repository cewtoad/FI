
@echo off
chcp 65001 >nul
title F1 Race Engineer - Web
cd /d "%~dp0"

rem Pick a Python. NOTE: the embedded full path is NEVER stored in PY and
rem never expanded inside a parenthesized block - a pack path like
rem "D:\new folder (3)\" would inject an unquoted ")" at block parse time and
rem kill the script even when the taken branch never used it (the actual
rem root cause of the "window flashes on other machines" report). goto keeps
rem only the taken branch parseable; the embedded launch is always the
rem quoted "%~dp0python.exe", which is safe for spaces and parentheses.
if exist "%~dp0python.exe" goto :run_embedded
where py >nul 2>nul && set "PY=py -3.12"
if defined PY goto :run_fallback
where python >nul 2>nul && set "PY=python"
if defined PY goto :run_fallback
echo [!] Python not found.
echo     Use the full pack - bundled runtime - or install Python 3.12.
pause
exit /b 1

:run_embedded
"%~dp0python.exe" FI.py --web
if errorlevel 1 pause
exit /b 0

:run_fallback
%PY% FI.py --web
if errorlevel 1 pause
exit /b 0
