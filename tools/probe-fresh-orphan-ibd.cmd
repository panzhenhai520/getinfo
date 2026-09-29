@echo off
setlocal
set BASE=D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013
set LOG=%BASE%\probe-orphan-ibd.log

echo ==== %date% %time% mysql tables ==== > "%LOG%"
docker exec ragflow-v0256-fresh-8013-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -N -e "SHOW TABLES;" >> "%LOG%" 2>&1

echo ==== %date% %time% rag_flow ibd files ==== >> "%LOG%"
dir /b "%BASE%\mysql_data\rag_flow\*.ibd" >> "%LOG%" 2>&1

endlocal
