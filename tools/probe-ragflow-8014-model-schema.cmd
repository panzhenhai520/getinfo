@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-model-schema-8014.log

echo ==== model schema %date% %time% ==== > "%LOG%"
docker exec ragflow_official_source_8014-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW CREATE TABLE tenant_llm\G SHOW CREATE TABLE tenant\G SHOW CREATE TABLE llm\G SHOW CREATE TABLE llm_factories\G" >> "%LOG%" 2>&1
