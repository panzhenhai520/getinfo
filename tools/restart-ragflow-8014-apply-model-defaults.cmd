@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-restart-apply-model-defaults-8014.log

echo ==== restart ragflow 8014 apply model defaults %date% %time% ==== > "%LOG%"
cd /d "%ROOT%\docker" || exit /b 1

docker compose -f docker-compose.yml up --pull never -d --force-recreate ragflow-cpu >> "%LOG%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo ==== compose ps ==== >> "%LOG%" 2>&1
docker compose -f docker-compose.yml ps >> "%LOG%" 2>&1
echo EXIT %EXITCODE% >> "%LOG%" 2>&1
exit /b %EXITCODE%
