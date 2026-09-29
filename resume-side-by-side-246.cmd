@echo off
setlocal
set DIR=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
cd /d "%DIR%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%DIR%\resume-side-by-side-246.ps1" > "%DIR%\resume-side-by-side-246.runner.log" 2>&1
exit /b %ERRORLEVEL%
