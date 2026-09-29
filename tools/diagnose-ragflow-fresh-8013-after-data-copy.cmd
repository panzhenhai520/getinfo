@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\diagnose-after-data-copy.log

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% docker ps ==== > "%LOG%"
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" >> "%LOG%" 2>&1

echo ==== %date% %time% docker compose ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% mysql logs ==== >> "%LOG%"
docker compose logs --tail=300 mysql >> "%LOG%" 2>&1

echo ==== %date% %time% es01 logs ==== >> "%LOG%"
docker compose logs --tail=160 es01 >> "%LOG%" 2>&1

echo ==== %date% %time% minio logs ==== >> "%LOG%"
docker compose logs --tail=120 minio >> "%LOG%" 2>&1

echo ==== %date% %time% ragflow-cpu logs ==== >> "%LOG%"
docker compose logs --tail=120 ragflow-cpu >> "%LOG%" 2>&1

endlocal
