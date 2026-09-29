@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set OUT=%ROOT%\codex-diagnose-container-8014.log
echo ==== diagnose %date% %time% ==== > "%OUT%"
echo ==== ps ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 ps aux >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== service_conf ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 bash -lc "sed -n '1,120p' /ragflow/conf/service_conf.yaml" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== entrypoint matching lines ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 bash -lc "grep -nE 'envsubst|service_conf|Initializing database|init_database' /ragflow/entrypoint.sh" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== ragflow logs dir ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 bash -lc "find /ragflow/logs -maxdepth 2 -type f -printf '%p %s\n' | sort" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== latest log tails ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 bash -lc "for f in /ragflow/logs/*; do [ -f \"$f\" ] && echo --- $f --- && tail -n 80 \"$f\"; done" >> "%OUT%" 2>&1
