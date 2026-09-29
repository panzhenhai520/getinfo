@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\start-old-246.log
echo === %date% %time% === > "%LOG%"
cd /d D:\docker-data\ragflow\ragflow >> "%LOG%" 2>&1
docker compose up -d >> "%LOG%" 2>&1
docker compose ps >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
