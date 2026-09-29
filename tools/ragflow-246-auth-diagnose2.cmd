@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\auth-diagnose2-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === likely auth files === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "find /ragflow/api/apps -maxdepth 4 -type f \( -iname '*auth*' -o -iname '*user*' \) | sort" >> "%LOG%" 2>&1
echo === likely service files === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "find /ragflow/api/db -maxdepth 4 -type f \( -iname '*user*' -o -iname '*account*' -o -iname '*tenant*' \) | sort" >> "%LOG%" 2>&1
echo === auth grep selected dirs === >> "%LOG%"
docker exec ragflow-v0256-upgrade-ragflow-cpu-1 sh -lc "grep -R \"auth/login\|login_channels\|def login\|def signin\|check_password\|decrypt\" -n /ragflow/api/apps/restful_apis /ragflow/api/apps/sdk /ragflow/api/db/services 2>/dev/null | head -200" >> "%LOG%" 2>&1
echo === recent filtered logs === >> "%LOG%"
docker compose logs --tail=300 ragflow-cpu | findstr /i "BadRequest auth login password user Xinference Ollama CONNECTION_ERROR NoSuchKey NoSuchBucket TypeError" >> "%LOG%" 2>&1
echo === user table columns === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW COLUMNS FROM user;" >> "%LOG%" 2>&1
echo === users summary === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,email,nickname,status,is_superuser,source,create_time,update_time FROM user ORDER BY create_time DESC LIMIT 20;" >> "%LOG%" 2>&1
echo === remaining suspect model refs === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT 'tenant_llm' AS tbl,id,tenant_id,llm_factory,model_type,llm_name,api_base,status FROM tenant_llm WHERE llm_factory='Xinference' OR api_base LIKE '%/v1' OR llm_name LIKE '%DeepSeek-R1-Distill-Llama-70B%' OR llm_name LIKE '%glm4:9b%'; SELECT 'dialog' AS tbl,id,tenant_id,name,llm_id,rerank_id,status FROM dialog WHERE llm_id LIKE '%Xinference%' OR llm_id LIKE '%DeepSeek-R1-Distill-Llama-70B%' OR llm_id LIKE '%glm4:9b%' OR rerank_id LIKE '%Xinference%';" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
