@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-native-8015
set LOG=%ROOT%\codex-configure-models-8015.log
set SQL=%ROOT%\configure-models-8015.sql

echo ==== configure ollama models native 8015 %date% %time% ==== > "%LOG%"
docker exec -i ragflow_official_native_8015-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow < "%SQL%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: model configuration SQL failed >> "%LOG%" 2>&1
  exit /b 1
)
echo ==== configure finished %date% %time% ==== >> "%LOG%" 2>&1
