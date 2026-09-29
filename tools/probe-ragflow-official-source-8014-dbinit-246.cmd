@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set OUT=%ROOT%\codex-dbinit-8014.log
echo ==== dbinit probe %date% %time% ==== > "%OUT%"
echo ==== process ==== >> "%OUT%"
docker exec ragflow_official_source_8014-ragflow-cpu-1 ps -o pid,ppid,stat,pcpu,pmem,etime,cmd -p 1,11 >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== mysql dbs ==== >> "%OUT%"
docker exec ragflow_official_source_8014-mysql-1 mysql -uroot -pinfini_rag_flow -e "show databases;" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== table count ==== >> "%OUT%"
docker exec ragflow_official_source_8014-mysql-1 mysql -uroot -pinfini_rag_flow rag_flow -e "select count(*) as table_count from information_schema.tables where table_schema='rag_flow'; show tables limit 20;" >> "%OUT%" 2>&1
