@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-configure-models-8014.log
set SQL=%ROOT%\configure-models-8014.sql

echo ==== configure ollama models %date% %time% ==== > "%LOG%"
docker exec -i ragflow_official_source_8014-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow < "%SQL%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: model configuration SQL failed >> "%LOG%" 2>&1
  exit /b 1
)
echo ==== configure finished %date% %time% ==== >> "%LOG%" 2>&1
