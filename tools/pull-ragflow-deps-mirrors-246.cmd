@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-pull-deps-mirrors.log

echo ==== pull deps mirrors started %date% %time% ==== > "%LOG%"

docker image inspect infiniflow/ragflow_deps:latest >> "%LOG%" 2>&1
if not errorlevel 1 (
  echo infiniflow/ragflow_deps:latest already exists. >> "%LOG%" 2>&1
  exit /b 0
)

for %%I in (
  registry.cn-hangzhou.aliyuncs.com/infiniflow/ragflow_deps:latest
  swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow_deps:latest
  registry.cn-hangzhou.aliyuncs.com/infiniflow/ragflow-deps:latest
  swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow-deps:latest
) do (
  echo ==== trying %%I ==== >> "%LOG%" 2>&1
  docker pull %%I >> "%LOG%" 2>&1
  if not errorlevel 1 (
    docker tag %%I infiniflow/ragflow_deps:latest >> "%LOG%" 2>&1
    if not errorlevel 1 (
      echo SUCCESS: tagged %%I as infiniflow/ragflow_deps:latest >> "%LOG%" 2>&1
      exit /b 0
    )
  )
)

echo ERROR: no mirror worked for ragflow_deps. >> "%LOG%" 2>&1
exit /b 1
