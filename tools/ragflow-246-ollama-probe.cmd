@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\ollama-probe-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === ollama tags === >> "%LOG%"
docker compose exec -T ragflow-cpu python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5).read().decode('utf-8','replace'))" >> "%LOG%" 2>&1
echo === ollama openai models === >> "%LOG%"
docker compose exec -T ragflow-cpu python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/v1/models', timeout=5).read().decode('utf-8','replace'))" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
