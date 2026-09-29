@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\chat-speed-fix-246.log
set BACKUP=%BASE%\codex-backups\pre-chat-speed-fix-dialog-cea3926e4ae611f0aaa71e2cb6df5e69.sql
cd /d "%BASE%"
if not exist "%BASE%\codex-backups" mkdir "%BASE%\codex-backups"
echo === %date% %time% === > "%LOG%"
echo === backup target dialog === >> "%LOG%"
docker compose exec -T mysql mysqldump -uroot -pinfini_rag_flow rag_flow dialog --where="id='cea3926e4ae611f0aaa71e2cb6df5e69'" > "%BACKUP%" 2>> "%LOG%"
echo backup=%BACKUP% >> "%LOG%"
echo === apply speed fix === >> "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "UPDATE dialog SET prompt_config=JSON_SET(CAST(prompt_config AS JSON), '$.cross_languages', JSON_ARRAY()), llm_setting=JSON_SET(CAST(llm_setting AS JSON), '$.max_tokens', 256) WHERE id='cea3926e4ae611f0aaa71e2cb6df5e69'; SELECT id,name,JSON_EXTRACT(prompt_config,'$.cross_languages') AS cross_languages,JSON_EXTRACT(llm_setting,'$.max_tokens') AS max_tokens,llm_setting FROM dialog WHERE id='cea3926e4ae611f0aaa71e2cb6df5e69';" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
