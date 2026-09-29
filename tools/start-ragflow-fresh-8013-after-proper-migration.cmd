@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\start-after-proper-migration.log

cd /d "%BASE%" || exit /b 1
echo ==== %date% %time% start ragflow-cpu ==== > "%LOG%"
docker compose up -d ragflow-cpu >> "%LOG%" 2>&1
echo ==== compose ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
