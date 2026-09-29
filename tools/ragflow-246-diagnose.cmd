@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === hostname === >> "%LOG%"
hostname >> "%LOG%" 2>&1
echo === whoami === >> "%LOG%"
whoami >> "%LOG%" 2>&1
echo === docker ps === >> "%LOG%"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" >> "%LOG%" 2>&1
echo === compose ps === >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo === ragflow-cpu logs === >> "%LOG%"
docker compose logs --tail=500 ragflow-cpu >> "%LOG%" 2>&1
echo === custom_server logs === >> "%LOG%"
docker compose logs --tail=200 custom_server >> "%LOG%" 2>&1
echo === mysql logs === >> "%LOG%"
docker compose logs --tail=120 mysql >> "%LOG%" 2>&1
echo === ragflow log files === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "ls -lah /ragflow/logs && for f in /ragflow/logs/*.log; do echo ==== $f ====; tail -n 160 $f; done" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
