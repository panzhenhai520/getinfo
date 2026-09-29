@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\minio-health.log
docker inspect --format="{{json .State.Health}}" ragflow-v0256-upgrade-minio-1 > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
