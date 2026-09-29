@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\model-probe-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === from host powershell ports === >> "%LOG%"
powershell -NoProfile -Command "foreach($p in 11434,9997){ $r=Test-NetConnection -ComputerName 127.0.0.1 -Port $p -WarningAction SilentlyContinue; \"$p TcpTestSucceeded=$($r.TcpTestSucceeded)\" }" >> "%LOG%" 2>&1
echo === ollama api tags from container === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "python - <<'PY'\nimport urllib.request\nfor url in ['http://host.docker.internal:11434/api/tags','http://host.docker.internal:11434/v1/models','http://host.docker.internal:9997/v1/models']:\n    print('URL', url)\n    try:\n        with urllib.request.urlopen(url, timeout=5) as r:\n            print(r.status, r.read(2000).decode('utf-8','replace'))\n    except Exception as e:\n        print(type(e).__name__, e)\nPY" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
