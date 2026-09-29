@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\codex-probe.log
cd /d "%BASE%" || exit /b 1
echo ==== %date% %time% compose ps ==== > "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo ==== %date% %time% docker ps ==== >> "%LOG%"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" >> "%LOG%" 2>&1
echo ==== %date% %time% ragflow-cpu logs ==== >> "%LOG%"
docker compose logs --tail=300 ragflow-cpu >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
endlocal
