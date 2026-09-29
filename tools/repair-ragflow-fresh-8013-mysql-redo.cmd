@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\repair-mysql-redo-after-data-copy.log
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set BACKUP=%BASE%\codex-backups\mysql-redo-mixed-%STAMP%

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% stop fresh mysql/ragflow ==== > "%LOG%"
docker compose stop ragflow-cpu mysql >> "%LOG%" 2>&1

echo ==== %date% %time% backup mixed redo/temp dirs ==== >> "%LOG%"
mkdir "%BACKUP%" >> "%LOG%" 2>&1
if exist "%BASE%\mysql_data\#innodb_redo" (
  move "%BASE%\mysql_data\#innodb_redo" "%BACKUP%\#innodb_redo" >> "%LOG%" 2>&1
)
if exist "%BASE%\mysql_data\#innodb_temp" (
  move "%BASE%\mysql_data\#innodb_temp" "%BACKUP%\#innodb_temp" >> "%LOG%" 2>&1
)

echo ==== %date% %time% docker compose up -d mysql ==== >> "%LOG%"
docker compose up -d mysql >> "%LOG%" 2>&1

echo ==== %date% %time% wait mysql health ==== >> "%LOG%"
for /l %%i in (1,1,40) do (
  docker inspect --format "{{.State.Health.Status}}" ragflow-v0256-fresh-8013-mysql-1 >> "%LOG%" 2>&1
  timeout /t 5 /nobreak >nul
)

echo ==== %date% %time% mysql ps/logs ==== >> "%LOG%"
docker compose ps mysql >> "%LOG%" 2>&1
docker compose logs --tail=160 mysql >> "%LOG%" 2>&1

echo ==== %date% %time% docker compose up -d all ==== >> "%LOG%"
docker compose up -d >> "%LOG%" 2>&1

echo ==== %date% %time% final ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% final logs ==== >> "%LOG%"
docker compose logs --tail=120 mysql ragflow-cpu >> "%LOG%" 2>&1

endlocal
