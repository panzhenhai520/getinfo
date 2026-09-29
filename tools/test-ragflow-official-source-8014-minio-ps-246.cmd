@echo off
setlocal
set OUT=D:\docker-data\ragflow\ragflow-official-source-8014\codex-minio-ps-test-8014.log
echo ==== minio ps test %date% %time% ==== > "%OUT%"
docker exec ragflow_official_source_8014-minio-1 sh -c "ps | grep '[m]inio' >/dev/null; echo ps_health_exit=$?" >> "%OUT%" 2>&1
