@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-apply-minio-health-8014.log
echo ==== apply minio health %date% %time% ==== > "%LOG%"
cd /d "%ROOT%\docker"
docker compose -f docker-compose.yml up --pull never -d minio >> "%LOG%" 2>&1
docker compose -f docker-compose.yml ps minio >> "%LOG%" 2>&1
