@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-source-8014
set LOG=%ROOT%\codex-build-start-8014.log

echo ==== build/start started %date% %time% ==== > "%LOG%"
echo ROOT=%ROOT% >> "%LOG%" 2>&1

if not exist "%ROOT%\Dockerfile.source8014" (
  echo ERROR: Dockerfile.source8014 not found at %ROOT%\Dockerfile.source8014 >> "%LOG%" 2>&1
  exit /b 1
)

cd /d "%ROOT%"
set DOCKER_BUILDKIT=1

echo ==== docker image inspect before build ==== >> "%LOG%" 2>&1
docker image inspect ragflow-official-source:v0.25.6-8014 >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ==== docker base image inspect ==== >> "%LOG%" 2>&1
  docker image inspect infiniflow/ragflow:v0.25.6 >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: required base image infiniflow/ragflow:v0.25.6 not found locally. >> "%LOG%" 2>&1
    exit /b 5
  )
  echo ==== docker build from official source ==== >> "%LOG%" 2>&1
  docker build --pull=false -f Dockerfile.source8014 -t ragflow-official-source:v0.25.6-8014 . >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: docker build failed %date% %time% >> "%LOG%" 2>&1
    exit /b 2
  )
) else (
  echo Image already exists; skipping build. >> "%LOG%" 2>&1
)

echo ==== docker compose config ==== >> "%LOG%" 2>&1
cd /d "%ROOT%\docker"
docker compose -f docker-compose.yml config --services >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: docker compose config failed %date% %time% >> "%LOG%" 2>&1
  exit /b 3
)

echo ==== docker compose up ==== >> "%LOG%" 2>&1
docker compose -f docker-compose.yml up --pull never -d >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: docker compose up failed %date% %time% >> "%LOG%" 2>&1
  exit /b 4
)

echo ==== docker compose ps ==== >> "%LOG%" 2>&1
docker compose -f docker-compose.yml ps >> "%LOG%" 2>&1
echo ==== build/start finished %date% %time% ==== >> "%LOG%" 2>&1
