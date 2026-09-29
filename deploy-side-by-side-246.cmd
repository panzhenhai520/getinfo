@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\deploy-side-by-side-246.runner.log
echo === %date% %time% === > "%LOG%"
where powershell >> "%LOG%" 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\deploy-side-by-side-246.ps1" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
