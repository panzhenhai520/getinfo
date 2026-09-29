@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\model-refs-query-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === tenant_llm suspects === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,status FROM tenant_llm WHERE llm_factory='Xinference' OR api_base LIKE '%/v1' OR llm_name LIKE '%DeepSeek-R1-Distill-Llama-70B%' OR llm_name LIKE '%glm4:9b%' ORDER BY id;" >> "%LOG%" 2>&1
echo === dialog suspects === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,rerank_id,status FROM dialog WHERE llm_id LIKE '%Xinference%' OR llm_id LIKE '%DeepSeek-R1-Distill-Llama-70B%' OR llm_id LIKE '%glm4:9b%' OR rerank_id LIKE '%Xinference%' ORDER BY update_time DESC;" >> "%LOG%" 2>&1
echo === current ollama tenant rows === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,status FROM tenant_llm WHERE llm_factory='Ollama' ORDER BY id;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
