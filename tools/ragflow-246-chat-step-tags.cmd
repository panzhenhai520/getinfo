@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\chat-step-tags-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === compose ps === >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo === copy json === >> "%LOG%"
docker cp "%BASE%\ollama-chat-test.json" ragflow-v0256-upgrade-ragflow-cpu-1:/tmp/ollama-chat-test.json >> "%LOG%" 2>&1
echo === ollama tags from container === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "curl -sS -m 10 http://host.docker.internal:11434/api/tags" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
