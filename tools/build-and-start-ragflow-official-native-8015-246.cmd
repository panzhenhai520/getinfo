@echo off
setlocal
set ROOT=D:\docker-data\ragflow\ragflow-official-native-8015
set LOG=%ROOT%\codex-build-start-8015.log

echo ==== build/start native 8015 started %date% %time% ==== > "%LOG%"
echo ROOT=%ROOT% >> "%LOG%" 2>&1

if not exist "%ROOT%\Dockerfile.source8015" (
  echo ERROR: Dockerfile.source8015 not found at %ROOT%\Dockerfile.source8015 >> "%LOG%" 2>&1
  exit /b 1
)

cd /d "%ROOT%"
set DOCKER_BUILDKIT=1

echo ==== docker image inspect before build ==== >> "%LOG%" 2>&1
docker image inspect ragflow-official-native:v0.25.6-8015 >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ==== docker base image inspect ==== >> "%LOG%" 2>&1
  docker image inspect infiniflow/ragflow:v0.25.6 >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: required base image infiniflow/ragflow:v0.25.6 not found locally. >> "%LOG%" 2>&1
    exit /b 5
  )
  echo ==== docker build from official native source ==== >> "%LOG%" 2>&1
  docker build --pull=false -f Dockerfile.source8015 -t ragflow-official-native:v0.25.6-8015 . >> "%LOG%" 2>&1
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
echo ==== build/start native 8015 finished %date% %time% ==== >> "%LOG%" 2>&1
