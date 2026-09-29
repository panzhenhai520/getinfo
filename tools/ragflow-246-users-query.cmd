@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-upgrade
set LOG=%BASE%\users-query-246.log
cd /d "%BASE%"
echo === %date% %time% === > "%LOG%"
docker compose exec -T mysql mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT id,email,nickname,status,is_active,is_authenticated,is_superuser,is_create_kownledge,create_time,update_time FROM user ORDER BY create_time DESC LIMIT 50;" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
