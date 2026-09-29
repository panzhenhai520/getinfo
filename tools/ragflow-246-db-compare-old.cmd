@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\db-compare-old-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === new dialog all === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 200;" >> "%LOG%" 2>&1
echo === new dialog count === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT COUNT(*) AS dialog_count FROM dialog;" >> "%LOG%" 2>&1
echo === old tenant_llm === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,max_tokens,status FROM tenant_llm ORDER BY tenant_id,model_type,llm_factory,llm_name;" >> "%LOG%" 2>&1
echo === old tenant === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,name,llm_id,embd_id,rerank_id,asr_id,img2txt_id,tts_id,status FROM tenant ORDER BY id;" >> "%LOG%" 2>&1
echo === old dialog refs === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 200;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
