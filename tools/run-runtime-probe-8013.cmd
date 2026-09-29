@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\runtime-probe-8013.log
cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% runtime probe ==== > "%LOG%"
docker compose cp runtime_probe_8013.py ragflow-cpu:/tmp/runtime_probe_8013.py >> "%LOG%" 2>&1
docker compose exec -T ragflow-cpu python3 /tmp/runtime_probe_8013.py >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
endlocal
