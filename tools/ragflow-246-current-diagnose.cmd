@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\current-diagnose-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === compose ps === >> "%LOG%"
docker compose ps >> "%LOG%" 2>&1
echo === tenant_llm current === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,llm_factory,model_type,llm_name,api_base,max_tokens,status FROM tenant_llm ORDER BY id;" >> "%LOG%" 2>&1
echo === tenant current === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,name,llm_id,embd_id,rerank_id,status FROM tenant ORDER BY id;" >> "%LOG%" 2>&1
echo === dialog all model refs === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,name,llm_id,rerank_id,status FROM dialog ORDER BY update_time DESC LIMIT 200;" >> "%LOG%" 2>&1
echo === canvas all model refs === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,tenant_id,title,dsl,status FROM canvas ORDER BY update_time DESC LIMIT 50;" >> "%LOG%" 2>&1
echo === recent ragflow logs === >> "%LOG%"
docker compose logs --tail=300 ragflow-cpu >> "%LOG%" 2>&1
echo === custom logs === >> "%LOG%"
docker compose logs --tail=100 custom_server >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
