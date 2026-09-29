@echo off
setlocal enabledelayedexpansion

set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set SRC=D:\docker-data\ragflow\ragflow-v0.25.6-offline-package-8013-final\ragflow-v0.25.6-offline
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set BACKUP=%BASE%\codex-backups\dirty-data-before-clean-restore-%STAMP%
set LOG=%BASE%\restore-clean-8013-%STAMP%.log

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% restore clean 8013 ==== > "%LOG%"
echo BASE=%BASE% >> "%LOG%"
echo SRC=%SRC% >> "%LOG%"
echo BACKUP=%BACKUP% >> "%LOG%"

echo ==== compose down ==== >> "%LOG%"
docker compose down >> "%LOG%" 2>&1
if errorlevel 1 goto fail

mkdir "%BACKUP%" >> "%LOG%" 2>&1

for %%D in (mysql_data minio_data esdata01 redis_data ragflow-logs) do (
  echo ==== backup %%D ==== >> "%LOG%"
  if exist "%BASE%\%%D" (
    move "%BASE%\%%D" "%BACKUP%\%%D" >> "%LOG%" 2>&1
    if errorlevel 1 goto fail
  )
)

for %%D in (mysql_data minio_data esdata01 redis_data ragflow-logs) do (
  echo ==== restore clean %%D ==== >> "%LOG%"
  mkdir "%BASE%\%%D" >> "%LOG%" 2>&1
  if exist "%SRC%\%%D" (
    robocopy "%SRC%\%%D" "%BASE%\%%D" /MIR /R:2 /W:2 >> "%LOG%" 2>&1
    if !ERRORLEVEL! GEQ 8 goto fail
  )
)

echo ==== compose up ==== >> "%LOG%"
docker compose up -d >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo ==== compose ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo SUCCESS backup=%BACKUP% >> "%LOG%"
exit /b 0

:fail
echo FAILED ERRORLEVEL=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
