@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-native-8015
set LOG=%ROOT%\codex-status-8015.log

echo ==== native 8015 status %date% %time% ==== > "%LOG%"
cd /d "%ROOT%\docker"
docker compose -f docker-compose.yml ps >> "%LOG%" 2>&1
echo ==== version endpoint ==== >> "%LOG%" 2>&1
curl.exe -sS http://127.0.0.1:8015/api/v1/system/version >> "%LOG%" 2>&1
echo. >> "%LOG%" 2>&1
echo ==== ragflow logs tail ==== >> "%LOG%" 2>&1
docker compose -f docker-compose.yml logs --tail 120 ragflow-cpu >> "%LOG%" 2>&1
