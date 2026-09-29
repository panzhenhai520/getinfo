@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\chat-stuck-diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === compose ps === >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo === copy ollama test json === >> "%LOG%"
docker cp "%BASE%\ollama-chat-test.json" ragflow-v0256-upgrade-ragflow-cpu-1:/tmp/ollama-chat-test.json >> "%LOG%" 2>&1
echo === container curl ollama tags === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "curl -sS -m 10 http://host.docker.internal:11434/api/tags" >> "%LOG%" 2>&1
echo.>> "%LOG%"
echo === container curl ollama chat === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "curl -sS -m 120 -H 'Content-Type: application/json' --data-binary @/tmp/ollama-chat-test.json http://host.docker.internal:11434/api/chat" >> "%LOG%" 2>&1
echo.>> "%LOG%"
echo === recent ragflow_server.log errors === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "tail -n 400 /ragflow/logs/ragflow_server.log | grep -Ei 'chat/completions|completion|ollama|litellm|connection|error|exception|traceback|unauthorized|deepseek|xinference|timeout' || true" >> "%LOG%" 2>&1
echo === recent docker logs === >> "%LOG%"
docker compose logs --tail=500 ragflow-cpu >> "%LOG%" 2>&1
echo === current active dialog model rows === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,tenant_llm_id,rerank_id,tenant_rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 30;" >> "%LOG%" 2>&1
echo === recent conversation/session rows === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW TABLES LIKE '%session%'; SHOW TABLES LIKE '%conversation%';" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
