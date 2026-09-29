@echo off
setlocal
set LOG=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade\new-db-columns.log
docker exec ragflow-v0256-upgrade-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -e "SHOW COLUMNS FROM user LIKE 'is_create_kownledge'; SHOW COLUMNS FROM dialog LIKE 'permission'; SHOW COLUMNS FROM conversation LIKE 'user_id';" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%
