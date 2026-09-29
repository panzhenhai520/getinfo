@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\minio-tools.log
docker exec ragflow-v0256-upgrade-minio-1 sh -c "command -v curl; command -v wget; command -v mc; command -v busybox" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
