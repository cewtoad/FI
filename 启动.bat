@echo off
chcp 65001 >nul
title F1 Race Engineer
cd /d "%~dp0"

echo ============================================
echo   F1 Race Engineer
echo ============================================
echo.

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

echo   启动中：浏览器面板将自动打开。
echo   保持本窗口开着；关掉窗口即停止。
echo.

rem Start the launcher in a child window, then open the browser.
start "F1TR-server" cmd /c "%PY%" FI.py --web --no-browser
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765"

echo.
echo   网页面板: http://127.0.0.1:8765
echo   若页面未加载，稍等几秒后刷新。
echo.
echo   按任意键停止并退出。
pause >nul

taskkill /FI "WINDOWTITLE eq F1TR-server*" /T /F >nul 2>nul
echo 已停止。
timeout /t 2 /nobreak >nul
exit /b 0
