@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\ollama-chat-probe-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === ollama tags === >> "%LOG%"
docker compose exec -T ragflow-cpu python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5).read().decode('utf-8','replace'))" >> "%LOG%" 2>&1
echo === ollama chat api === >> "%LOG%"
docker compose exec -T ragflow-cpu python -c "import urllib.request,json; body=json.dumps({'model':'deepseek-r1:1.5b','messages':[{'role':'user','content':'hello, answer with one short sentence'}],'stream':False}).encode(); req=urllib.request.Request('http://host.docker.internal:11434/api/chat', data=body, headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req, timeout=120).read().decode('utf-8','replace')[:4000])" >> "%LOG%" 2>&1
echo === ragflow logs tail === >> "%LOG%"
docker compose logs --tail=300 ragflow-cpu >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
