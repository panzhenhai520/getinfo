@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\repair-system-settings-orphan.log
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%%time:~6%
set STAMP=%STAMP: =0%
set BACKUP=%BASE%\codex-backups\orphan-ibd-%STAMP%

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% stop fresh ragflow/mysql ==== > "%LOG%"
docker compose stop ragflow-cpu mysql >> "%LOG%" 2>&1

echo ==== %date% %time% move orphan system_settings.ibd ==== >> "%LOG%"
mkdir "%BACKUP%" >> "%LOG%" 2>&1
if exist "%BASE%\mysql_data\rag_flow\system_settings.ibd" (
  move "%BASE%\mysql_data\rag_flow\system_settings.ibd" "%BACKUP%\system_settings.ibd" >> "%LOG%" 2>&1
)

echo ==== %date% %time% start fresh all ==== >> "%LOG%"
docker compose up -d >> "%LOG%" 2>&1

echo ==== %date% %time% wait short ==== >> "%LOG%"
timeout /t 20 /nobreak >nul

echo ==== %date% %time% final ps ==== >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% logs ==== >> "%LOG%"
docker compose logs --tail=180 mysql ragflow-cpu >> "%LOG%" 2>&1

endlocal
