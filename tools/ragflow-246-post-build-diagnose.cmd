@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\post-build-diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === docker ps === >> "%LOG%"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" >> "%LOG%" 2>&1
echo === compose ps === >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo === ragflow-cpu logs tail === >> "%LOG%"
docker compose logs --tail=240 ragflow-cpu >> "%LOG%" 2>&1
echo === container processes === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "ps aux | head -40; echo === nginx conf custom ===; nginx -T 2>&1 | grep -n -E 'custom|proxy_pass|ragflow.conf' | head -80; echo === document patch ===; grep -n 'Document file not found in storage' /ragflow/api/apps/restful_apis/document_api.py || true" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
