@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\db-diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === tenant_llm === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,max_tokens,status FROM tenant_llm ORDER BY tenant_id,model_type,llm_factory,llm_name;" >> "%LOG%" 2>&1
echo === llm xinference/ollama === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT fid,llm_name,model_type,tags,max_tokens,status FROM llm WHERE fid IN ('Xinference','Ollama') OR llm_name LIKE '%DeepSeek%' OR llm_name LIKE '%deepseek%' ORDER BY fid,model_type,llm_name LIMIT 200;" >> "%LOG%" 2>&1
echo === tenant === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,name,llm_id,embd_id,rerank_id,asr_id,img2txt_id,tts_id,status FROM tenant ORDER BY id;" >> "%LOG%" 2>&1
echo === dialog model refs === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,rerank_id,status FROM dialog WHERE llm_id LIKE '%DeepSeek%' OR llm_id LIKE '%deepseek%' OR llm_id LIKE '%Ollama%' OR llm_id LIKE '%Xinference%' ORDER BY update_time DESC LIMIT 100;" >> "%LOG%" 2>&1
echo === knowledgebase model refs === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,embd_id,status FROM knowledgebase WHERE embd_id LIKE '%Xinference%' OR embd_id LIKE '%Ollama%' OR embd_id LIKE '%bge%' ORDER BY update_time DESC LIMIT 100;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
