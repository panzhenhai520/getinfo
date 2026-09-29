@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set OUT=%ROOT%\codex-minio-health-8014.log
echo ==== minio health %date% %time% ==== > "%OUT%"
docker inspect --format="{{json .State.Health}}" ragflow_official_source_8014-minio-1 >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== which curl/wget ==== >> "%OUT%"
docker exec ragflow_official_source_8014-minio-1 sh -c "which curl; curl -f http://localhost:9000/minio/health/live; echo curl_exit=$?; which wget; wget -qO- http://localhost:9000/minio/health/live; echo wget_exit=$?" >> "%OUT%" 2>&1
