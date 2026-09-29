@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\restart-after-data-copy.log

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% docker compose ps before ==== > "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% docker compose down ==== >> "%LOG%"
docker compose down >> "%LOG%" 2>&1 || exit /b 1

echo ==== %date% %time% docker compose up -d ==== >> "%LOG%"
docker compose up -d >> "%LOG%" 2>&1 || exit /b 1

echo ==== %date% %time% docker compose ps after ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% docker compose logs tail ==== >> "%LOG%"
docker compose logs --tail=120 mysql es01 minio ragflow-cpu >> "%LOG%" 2>&1

endlocal
