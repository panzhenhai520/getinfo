@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-ollama-container-8014.log

echo ==== ollama from ragflow container %date% %time% ==== > "%LOG%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 /ragflow/.venv/bin/python -c "import urllib.request; urls=['http://host.docker.internal:11434/api/tags','http://192.168.1.246:11434/api/tags']; [print(u, urllib.request.urlopen(u, timeout=5).status, urllib.request.urlopen(u, timeout=5).read(300).decode('utf-8','ignore')) for u in urls]" >> "%LOG%" 2>&1
