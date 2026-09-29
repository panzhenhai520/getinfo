@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\minio-ready.log
docker exec ragflow-v0256-upgrade-minio-1 mc ready local > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
