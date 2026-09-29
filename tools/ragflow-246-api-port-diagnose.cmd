@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\api-port-diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === inside curl root/version === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "curl -v --max-time 10 http://127.0.0.1:9380/api/v1/system/version; echo; curl -v --max-time 10 http://127.0.0.1:9380/; echo; curl -v --max-time 10 http://127.0.0.1:80/api/v1/system/version; echo" >> "%LOG%" 2>&1
echo === sockets === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "python3 - <<'PY'\nimport socket\nfor port in [80,9380,9381]:\n    s=socket.socket(); s.settimeout(3)\n    try:\n        s.connect(('127.0.0.1', port)); print(port, 'connect ok')\n        s.sendall(b'GET /api/v1/system/version HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n')\n        print(port, s.recv(200))\n    except Exception as e:\n        print(port, type(e).__name__, e)\n    finally:\n        s.close()\nPY" >> "%LOG%" 2>&1
echo === recent server log request lines === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "tail -n 80 /ragflow/logs/ragflow_server.log" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
