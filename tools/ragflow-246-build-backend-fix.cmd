@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\build-backend-fix-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === docker build custom backend === >> "%LOG%"
docker build --build-arg BASE_IMAGE=infiniflow/ragflow:v0.25.6 -t ragflow:v0.25.6-custom .\backend-image >> "%LOG%" 2>&1
echo build_exit=%ERRORLEVEL% >> "%LOG%"
if errorlevel 1 exit /b %ERRORLEVEL%
echo === recreate ragflow-cpu === >> "%LOG%"
docker compose up -d --no-deps --force-recreate ragflow-cpu >> "%LOG%" 2>&1
echo recreate_exit=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
