@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\nginx-reload-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
docker compose exec -T ragflow-cpu nginx -t >> "%LOG%" 2>&1
echo nginx_test_exit=%ERRORLEVEL% >> "%LOG%"
if errorlevel 1 exit /b %ERRORLEVEL%
docker compose exec -T ragflow-cpu nginx -s reload >> "%LOG%" 2>&1
echo nginx_reload_exit=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
