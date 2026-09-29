@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\remote-check-246.log
echo === %date% %time% === > "%LOG%"
hostname >> "%LOG%" 2>&1
whoami >> "%LOG%" 2>&1
ver >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === where docker === >> "%LOG%"
where docker >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === docker version === >> "%LOG%"
docker version >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === docker compose version === >> "%LOG%"
docker compose version >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === docker ps === >> "%LOG%"
docker ps >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo === upgrade dir === >> "%LOG%"
dir D:\docker-data\ragflow\ragflow-v0.25.6-upgrade >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
