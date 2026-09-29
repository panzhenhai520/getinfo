@echo off
setlocal
set DIR=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%DIR%\apply-new-minio-health.log
cd /d "%DIR%"
docker compose up -d minio > "%LOG%" 2>&1
docker inspect --format="{{.State.Health.Status}}" ragflow-v0256-upgrade-minio-1 >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
