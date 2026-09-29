@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-native-8015
set LOG=%ROOT%\codex-api-diagnose-8015.log
set C=ragflow_official_native_8015-ragflow-cpu-1

echo ==== native 8015 api diagnose %date% %time% ==== > "%LOG%"
echo ==== compose ps ==== >> "%LOG%" 2>&1
cd /d "%ROOT%\docker"
docker compose -f docker-compose.yml ps >> "%LOG%" 2>&1

echo ==== process list ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "ps -ef" >> "%LOG%" 2>&1

echo ==== listening ports ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null || true" >> "%LOG%" 2>&1

echo ==== local curl 9380 ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "curl -sS -m 8 http://127.0.0.1:9380/v1/system/version || true" >> "%LOG%" 2>&1

echo ==== local curl 9381 ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "curl -sS -m 8 http://127.0.0.1:9381/ || true" >> "%LOG%" 2>&1

echo ==== ragflow server log tail ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "tail -n 220 /ragflow/logs/ragflow_server.log 2>/dev/null || true" >> "%LOG%" 2>&1

echo ==== admin log tail ==== >> "%LOG%" 2>&1
docker exec %C% sh -lc "tail -n 120 /ragflow/logs/admin_service.log 2>/dev/null || true" >> "%LOG%" 2>&1
