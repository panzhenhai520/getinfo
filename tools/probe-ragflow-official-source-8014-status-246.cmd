@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set OUT=%ROOT%\codex-status-8014.log
echo ==== status %date% %time% ==== > "%OUT%"
cd /d "%ROOT%\docker"
docker compose -f docker-compose.yml ps >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== containers ==== >> "%OUT%"
docker ps -a --filter "name=ragflow_official_source_8014" --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== recent ragflow logs ==== >> "%OUT%"
docker logs --tail 120 ragflow_official_source_8014-ragflow-cpu-1 >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== mysql health logs ==== >> "%OUT%"
docker inspect --format="{{json .State.Health}}" ragflow_official_source_8014-mysql-1 >> "%OUT%" 2>&1
