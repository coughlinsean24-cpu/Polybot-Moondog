@echo off
title Polybot Snipez - Web Dashboard
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

REM Kill any old instance before starting
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and CommandLine like '%%web_dashboard%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
    echo Killing old bot instance PID %%i ...
    taskkill /F /PID %%i >nul 2>&1
)

echo ================================================
echo   POLYBOT SNIPEZ -- Web Dashboard
echo   Opening http://localhost:5050 in browser...
echo ================================================
echo.

timeout /t 2 /nobreak >nul
start http://localhost:5050

python web_dashboard.py

echo.
echo Dashboard stopped. Check logs\ folder for details.
pause
