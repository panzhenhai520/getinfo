@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\nginx-inspect-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === host nginx ragflow.conf === >> "%LOG%"
type "%BASE%\nginx\ragflow.conf" >> "%LOG%" 2>&1
echo === compose config ragflow-cpu volumes === >> "%LOG%"
docker compose config >> "%LOG%" 2>&1
echo === container nginx -T custom/api/server snippets === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "nginx -T 2>&1 | grep -n -E 'server_name|custom|location|proxy_pass|ragflow.conf|conf.d' | head -n 240" >> "%LOG%" 2>&1
echo === container nginx conf files === >> "%LOG%"
docker compose exec -T ragflow-cpu sh -lc "find /etc/nginx /ragflow -maxdepth 4 -type f \( -name '*.conf' -o -name 'nginx.conf' \) -print 2>/dev/null | sort" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
