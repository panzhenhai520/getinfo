@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\repair-mysql-empty-redo-after-data-copy.log

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% stop fresh mysql/ragflow ==== > "%LOG%"
docker compose stop ragflow-cpu mysql >> "%LOG%" 2>&1

echo ==== %date% %time% create empty redo/temp dirs ==== >> "%LOG%"
if not exist "%BASE%\mysql_data\#innodb_redo" mkdir "%BASE%\mysql_data\#innodb_redo" >> "%LOG%" 2>&1
if not exist "%BASE%\mysql_data\#innodb_temp" mkdir "%BASE%\mysql_data\#innodb_temp" >> "%LOG%" 2>&1

echo ==== %date% %time% docker compose up -d mysql ==== >> "%LOG%"
docker compose up -d mysql >> "%LOG%" 2>&1

echo ==== %date% %time% wait mysql health ==== >> "%LOG%"
for /l %%i in (1,1,30) do (
  docker inspect --format "{{.State.Health.Status}}" ragflow-v0256-fresh-8013-mysql-1 >> "%LOG%" 2>&1
  timeout /t 5 /nobreak >nul
)

echo ==== %date% %time% docker compose up -d all ==== >> "%LOG%"
docker compose up -d >> "%LOG%" 2>&1

echo ==== %date% %time% final ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% final logs ==== >> "%LOG%"
docker compose logs --tail=160 mysql ragflow-cpu >> "%LOG%" 2>&1

endlocal
