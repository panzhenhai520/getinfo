@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\active-dialog-settings-probe.log
set MYSQL_PWD=infini_rag_flow

cd /d "%BASE%" || exit /b 1
echo ==== %date% %time% active dialog settings ==== > "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,name,llm_id,tenant_llm_id,status,LEFT(llm_setting,1200) AS llm_setting,LEFT(prompt_config,1800) AS prompt_config FROM dialog WHERE status=1 ORDER BY update_time DESC;" >> "%LOG%" 2>&1
echo ==== recent ragflow server errors ==== >> "%LOG%"
docker compose logs --tail=260 ragflow-cpu >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
