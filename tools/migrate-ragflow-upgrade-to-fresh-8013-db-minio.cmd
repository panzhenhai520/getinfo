@echo off
setlocal enabledelayedexpansion

set SRC=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set DST=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set MYSQL_PWD=infini_rag_flow
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set WORK=%DST%\codex-backups\proper-migration-from-upgrade-%STAMP%
set LOG=%WORK%\db-minio-migration.log

mkdir "%WORK%" || exit /b 1
echo ==== %date% %time% proper migration db+minio ==== > "%LOG%"
echo SRC=%SRC% >> "%LOG%"
echo DST=%DST% >> "%LOG%"
echo WORK=%WORK% >> "%LOG%"

echo ==== source mysql counts ==== >> "%LOG%"
cd /d "%SRC%" || goto fail
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT 'user' table_name, COUNT(*) count FROM user UNION ALL SELECT 'knowledgebase', COUNT(*) FROM knowledgebase UNION ALL SELECT 'document', COUNT(*) FROM document UNION ALL SELECT 'dialog', COUNT(*) FROM dialog;" >> "%LOG%" 2>&1

echo ==== dump source mysql ==== >> "%LOG%"
docker compose exec -T mysql mysqldump -uroot -p%MYSQL_PWD% --single-transaction --quick --routines --triggers --events --hex-blob --default-character-set=utf8mb4 --set-gtid-purged=OFF rag_flow > "%WORK%\rag_flow.sql" 2>> "%LOG%"
if errorlevel 1 goto fail

for %%F in ("%WORK%\rag_flow.sql") do echo dump_size=%%~zF >> "%LOG%"

echo ==== stop target ragflow and minio ==== >> "%LOG%"
cd /d "%DST%" || goto fail
docker compose stop ragflow-cpu >> "%LOG%" 2>&1
docker compose stop minio >> "%LOG%" 2>&1

echo ==== reset and import target mysql ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% -e "DROP DATABASE IF EXISTS rag_flow; CREATE DATABASE rag_flow DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow < "%WORK%\rag_flow.sql" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo ==== target mysql counts ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT 'user' table_name, COUNT(*) count FROM user UNION ALL SELECT 'knowledgebase', COUNT(*) FROM knowledgebase UNION ALL SELECT 'document', COUNT(*) FROM document UNION ALL SELECT 'dialog', COUNT(*) FROM dialog;" >> "%LOG%" 2>&1

echo ==== backup target minio_data ==== >> "%LOG%"
if exist "%DST%\minio_data" (
  move "%DST%\minio_data" "%WORK%\target-minio-before-import" >> "%LOG%" 2>&1
  if errorlevel 1 goto fail
)

echo ==== mirror source minio_data to target ==== >> "%LOG%"
robocopy "%SRC%\minio_data" "%DST%\minio_data" /MIR /R:2 /W:2 >> "%LOG%" 2>&1
if !ERRORLEVEL! GEQ 8 goto fail

echo ==== start target infrastructure only ==== >> "%LOG%"
docker compose up -d mysql minio redis es01 >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo ==== target compose ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo SUCCESS work=%WORK% >> "%LOG%"
exit /b 0

:fail
echo FAILED ERRORLEVEL=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
