@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\dialog-cea-hex-probe.log
set MYSQL_PWD=infini_rag_flow
set TARGET=cea3926e4ae611f0aaa71e2cb6df5e69

cd /d "%BASE%" || exit /b 1
echo ==== %date% %time% dialog prompt hex ==== > "%LOG%"
docker compose exec -T mysql mysql -uroot -p%MYSQL_PWD% rag_flow -e "SELECT CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(prompt_config,'$.system'))) AS chars, HEX(SUBSTRING(JSON_UNQUOTE(JSON_EXTRACT(prompt_config,'$.system')),1,8)) AS first8_hex, JSON_EXTRACT(prompt_config,'$.parameters') parameters, JSON_EXTRACT(prompt_config,'$.cross_languages') cross_languages FROM dialog WHERE id='%TARGET%';" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
