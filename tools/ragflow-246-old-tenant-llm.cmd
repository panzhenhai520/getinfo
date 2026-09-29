@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\old-tenant-llm-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
echo === old tenant_llm columns === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW COLUMNS FROM tenant_llm;" >> "%LOG%" 2>&1
echo === old tenant_llm rows === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT * FROM tenant_llm ORDER BY tenant_id,model_type,llm_factory,llm_name;" >> "%LOG%" 2>&1
echo === old llm rows === >> "%LOG%"
docker exec ragflow-mysql2 mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT * FROM llm WHERE fid IN ('Xinference','Ollama') OR llm_name LIKE '%DeepSeek%' OR llm_name LIKE '%deepseek%' ORDER BY fid,model_type,llm_name LIMIT 200;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
