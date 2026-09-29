@echo off
cd /d F:\CollectInfo

docker info >nul 2>&1
if errorlevel 1 (
    echo Docker engine is not running.
    echo I will open Docker Desktop for you.
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo When Docker Desktop shows "Engine running", come back here and press any key.
    pause
)

echo Starting crawler services...
docker compose -f docker-compose.crawler.yml up -d
if errorlevel 1 (
    echo Failed to start. Please copy the red error above and send it to me.
    pause
    exit /b 1
)

echo Started. Opening http://localhost:8004 ...
timeout /t 10 /nobreak >nul
start http://localhost:8004
pause
