@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-native-8015
set LOG=%ROOT%\codex-models-8015.log

echo ==== native 8015 models %date% %time% ==== > "%LOG%"
docker exec ragflow_official_native_8015-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,email,nickname,status FROM user ORDER BY create_time DESC; SELECT id,name,llm_id,embd_id,tenant_llm_id,tenant_embd_id,tenant_rerank_id FROM tenant; SELECT id,tenant_id,llm_factory,llm_name,model_type,api_base,status,max_tokens FROM tenant_llm ORDER BY tenant_id,llm_factory,model_type,llm_name;" >> "%LOG%" 2>&1
