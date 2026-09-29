@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\fix-dialog-cea-repeat.log
set BACKUP_DIR=%BASE%\codex-backups
set BACKUP=%BACKUP_DIR%\dialog-cea-before-repeat-fix-20260608.sql
set MYSQL_PWD=infini_rag_flow
set TARGET=cea3926e4ae611f0aaa71e2cb6df5e69
set SOURCE=47025c4e4b5611f08dbc1a4387aedd86

cd /d "%BASE%" || exit /b 1
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

echo ==== %date% %time% fix dialog repeat ==== > "%LOG%"
echo target=%TARGET% source_prompt=%SOURCE% >> "%LOG%"

echo ==== backup target dialog ==== >> "%LOG%"
docker compose exec -T mysql mysqldump -uroot -p%MYSQL_PWD% rag_flow dialog --where="id='%TARGET%'" > "%BACKUP%" 2>> "%LOG%"
if errorlevel 1 goto fail
echo backup=%BACKUP% >> "%LOG%"

echo ==== before ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,name,llm_id,tenant_llm_id,LEFT(llm_setting,600) llm_setting,JSON_EXTRACT(prompt_config,'$.parameters') parameters,JSON_EXTRACT(prompt_config,'$.cross_languages') cross_languages,LEFT(JSON_UNQUOTE(JSON_EXTRACT(prompt_config,'$.system')),120) system_head FROM dialog WHERE id='%TARGET%';" >> "%LOG%" 2>&1

echo ==== apply ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "UPDATE dialog t JOIN dialog s ON s.id='%SOURCE%' SET t.prompt_config=JSON_SET(CAST(s.prompt_config AS JSON), '$.refine_multiturn', CAST('true' AS JSON), '$.reasoning', CAST('false' AS JSON), '$.cross_languages', JSON_ARRAY('Chinese','English','Cantonese'), '$.parameters', JSON_ARRAY(JSON_OBJECT('key','knowledge','optional',CAST('false' AS JSON))), '$.system', CONCAT(JSON_UNQUOTE(JSON_EXTRACT(s.prompt_config,'$.system')), '\n\nDo not repeat the same sentence or phrase. Answer in concise Chinese, at most three bullet points.')), t.llm_setting=CAST('{\"model_type\":\"chat\",\"temperature\":0.05,\"top_p\":0.2,\"presence_penalty\":0.2,\"frequency_penalty\":0.8,\"max_tokens\":220}' AS JSON) WHERE t.id='%TARGET%';" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo ==== after ==== >> "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT id,name,llm_id,tenant_llm_id,LEFT(llm_setting,600) llm_setting,JSON_EXTRACT(prompt_config,'$.parameters') parameters,JSON_EXTRACT(prompt_config,'$.cross_languages') cross_languages,LEFT(JSON_UNQUOTE(JSON_EXTRACT(prompt_config,'$.system')),180) system_head FROM dialog WHERE id='%TARGET%';" >> "%LOG%" 2>&1

echo SUCCESS >> "%LOG%"
exit /b 0

:fail
echo FAILED ERRORLEVEL=%ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
