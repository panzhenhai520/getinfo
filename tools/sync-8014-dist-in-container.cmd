@echo off
setlocal

set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-sync-dist-8014.log
set CONTAINER=ragflow_official_source_8014-ragflow-cpu-1
set BACKUP_SUFFIX=20260609_2258

echo ==== sync dist started %date% %time% ==== > "%LOG%"
echo ROOT=%ROOT% >> "%LOG%"
echo CONTAINER=%CONTAINER% >> "%LOG%"

docker exec %CONTAINER% sh -lc "echo container-ready" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: container is not reachable: %CONTAINER% >> "%LOG%"
  exit /b 2
)

docker exec %CONTAINER% sh -lc "set -e; if [ -d /ragflow/web/dist ]; then mv /ragflow/web/dist /ragflow/web/dist_backup_before_8014_%BACKUP_SUFFIX%; fi; mkdir -p /ragflow/web/dist" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: failed to prepare container dist directory >> "%LOG%"
  exit /b 3
)

docker cp "%ROOT%\web\dist\." %CONTAINER%:/ragflow/web/dist >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: docker cp dist failed >> "%LOG%"
  exit /b 4
)

echo ==== container dist scan ==== >> "%LOG%"
docker exec %CONTAINER% sh -lc "find /ragflow/web/dist -name '*.map' -print | head -5; grep -R -n -m 20 -E 'RAGFlow|ragflow|ragflow\.io|Powered by|react-dev-inspector|data-inspector' /ragflow/web/dist || true" >> "%LOG%" 2>&1

echo ==== sync dist finished %date% %time% ==== >> "%LOG%"
