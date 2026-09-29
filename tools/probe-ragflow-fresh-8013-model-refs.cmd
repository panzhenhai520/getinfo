@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\model-refs-after-proper-migration.log
set MYSQL_PWD=infini_rag_flow

cd /d "%BASE%" || exit /b 1
echo ==== %date% %time% tenant_llm ==== > "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,status FROM tenant_llm ORDER BY tenant_id,model_type,llm_name;" >> "%LOG%" 2>&1
echo ==== tenant ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,name,llm_id,embd_id,rerank_id,status FROM tenant ORDER BY id;" >> "%LOG%" 2>&1
echo ==== dialog refs ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,name,llm_id,tenant_llm_id,rerank_id,tenant_rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 80;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
