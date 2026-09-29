@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\smoke-246.log
cd /d "%BASE%"
echo smoke %date% %time% > "%LOG%"
echo cd_error %ERRORLEVEL% >> "%LOG%"
dir "%BASE%" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
