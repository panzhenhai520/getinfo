@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-verify-model-runtime-8014.log
set PROBE=%ROOT%\verify_ragflow_8014_models.py

echo ==== verify ragflow 8014 model runtime %date% %time% ==== > "%LOG%"
cd /d "%ROOT%" || exit /b 1

docker cp "%PROBE%" ragflow_official_source_8014-ragflow-cpu-1:/tmp/verify_ragflow_8014_models.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: docker cp failed >> "%LOG%" 2>&1
  exit /b 1
)

docker exec -w /ragflow ragflow_official_source_8014-ragflow-cpu-1 /ragflow/.venv/bin/python /tmp/verify_ragflow_8014_models.py >> "%LOG%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo EXIT %EXITCODE% >> "%LOG%" 2>&1
exit /b %EXITCODE%
