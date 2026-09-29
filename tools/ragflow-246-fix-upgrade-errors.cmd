@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\fix-upgrade-errors-246.log
set BACKUP_DIR=%BASE%\codex-backups
set BACKUP=%BACKUP_DIR%\pre-fix-upgrade-errors-%RANDOM%.sql
set SQL=%BASE%\fix-upgrade-errors.sql
cd /d "%BASE%"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
echo === %date% %time% === > "%LOG%"
echo === backup tenant tenant_llm dialog === >> "%LOG%"
docker compose exec -T mysql mysqldump --single-transaction -uroot -pinfini_rag_flow rag_flow tenant tenant_llm dialog > "%BACKUP%" 2>> "%LOG%"
echo backup_file=%BACKUP% >> "%LOG%"
echo backup_exit=%ERRORLEVEL% >> "%LOG%"
if errorlevel 1 exit /b %ERRORLEVEL%
echo === apply sql === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow < "%SQL%" >> "%LOG%" 2>&1
echo sql_exit=%ERRORLEVEL% >> "%LOG%"
if errorlevel 1 exit /b %ERRORLEVEL%
echo === restart ragflow-cpu === >> "%LOG%"
docker compose restart ragflow-cpu >> "%LOG%" 2>&1
echo restart_exit=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
