@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\backend-port-diagnose-after-restart.log

cd /d "%BASE%" || exit /b 1

echo ==== %date% %time% compose ps ==== > "%LOG%"
docker compose ps >> "%LOG%" 2>&1

echo ==== %date% %time% host ports ==== >> "%LOG%"
curl -s -o NUL -w "8013=/ %%{http_code}\n" http://127.0.0.1:8013/ >> "%LOG%" 2>&1
curl -s -o NUL -w "8013 version=%%{http_code}\n" http://127.0.0.1:8013/api/v1/system/version >> "%LOG%" 2>&1
curl -s -o NUL -w "29380 version=%%{http_code}\n" http://127.0.0.1:29380/api/v1/system/version >> "%LOG%" 2>&1

echo ==== %date% %time% container sockets ==== >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "python3 - <<'PY'
import socket
for port in [80, 9380, 9381, 9382, 9383, 9384]:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(('127.0.0.1', port))
        print(port, 'connect ok')
        s.sendall(b'GET /api/v1/system/version HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        print(port, s.recv(300))
    except Exception as e:
        print(port, type(e).__name__, e)
    finally:
        s.close()
PY" >> "%LOG%" 2>&1

echo ==== %date% %time% process list ==== >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "ps -ef | sed -n '1,220p'" >> "%LOG%" 2>&1

echo ==== %date% %time% log files ==== >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "ls -la /ragflow/logs 2>/dev/null || true; find /ragflow/logs -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' 2>/dev/null | sort" >> "%LOG%" 2>&1

echo ==== %date% %time% ragflow_server.log tail ==== >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "tail -n 220 /ragflow/logs/ragflow_server.log 2>/dev/null || true" >> "%LOG%" 2>&1

echo ==== %date% %time% docker ragflow-cpu logs ==== >> "%LOG%"
docker compose logs --tail=260 ragflow-cpu >> "%LOG%" 2>&1

echo EXIT %ERRORLEVEL% >> "%LOG%"
endlocal
