@echo off
setlocal
set LOG=D:\docker-data\ragflow\probe-ragflow-mysql-sources.log

echo ==== %date% %time% source mysql probes ==== > "%LOG%"

for %%C in (ragflow-mysql2 ragflow-v0256-upgrade-mysql-1 ragflow-v0256-fresh-8013-mysql-1) do (
  echo ==== %%C ==== >> "%LOG%"
  docker exec %%C mysql -uroot -pinfini_rag_flow rag_flow -e "SELECT DATABASE() AS db; SHOW TABLES LIKE 'user'; SHOW TABLES LIKE 'knowledgebase'; SHOW TABLES LIKE 'document'; SELECT 'user' AS tbl, COUNT(*) AS cnt FROM user; SELECT 'knowledgebase' AS tbl, COUNT(*) AS cnt FROM knowledgebase; SELECT 'document' AS tbl, COUNT(*) AS cnt FROM document; SELECT 'dialog' AS tbl, COUNT(*) AS cnt FROM dialog;" >> "%LOG%" 2>&1
)

endlocal
