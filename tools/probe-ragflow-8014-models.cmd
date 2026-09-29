@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-models-8014.log

echo ==== model probe %date% %time% ==== > "%LOG%"
docker exec ragflow_official_source_8014-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,email,nickname,status FROM user ORDER BY create_time DESC; SELECT id,name,llm_id,embd_id,tenant_llm_id,tenant_embd_id,tenant_rerank_id FROM tenant; SELECT id,tenant_id,llm_factory,llm_name,model_type,api_base,status,max_tokens FROM tenant_llm ORDER BY tenant_id,llm_factory,model_type,llm_name;" >> "%LOG%" 2>&1
