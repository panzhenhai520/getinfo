@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\chat-config-query-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === dialog columns === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW COLUMNS FROM dialog;" >> "%LOG%" 2>&1
echo === target chat row === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,name,llm_id,tenant_llm_id,rerank_id,tenant_rerank_id,status,LEFT(prompt_config,2000) AS prompt_config_head,LEFT(llm_setting,1000) AS llm_setting_head FROM dialog WHERE id='cea3926e4ae611f0aaa71e2cb6df5e69';" >> "%LOG%" 2>&1
echo === tenant llm rows === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,max_tokens,status FROM tenant_llm ORDER BY id;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
