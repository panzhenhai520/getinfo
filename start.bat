@echo off
chcp 65001 >nul
title 情报系统启动（含爬虫）
cd /d %~dp0

echo ============================================================
echo  情报系统 启动（完整模式：爬虫调度 + Flask 0.0.0.0:8003）
echo ============================================================
echo.

echo [1/3] 结束旧的 8003 服务进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'start_with_schedule|run_flask' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  已停止 PID ' $_.ProcessId }"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8003 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host '  已停止 PID ' $_.OwningProcess ' (端口8003)' }"
timeout /t 2 >nul

echo [2/3] 启动完整服务（start_with_schedule.py）...
start "情报系统-8003" /D "%~dp0" "C:\Anaconda\python.exe" start_with_schedule.py

echo [3/3] 等待服务就绪...
timeout /t 6 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8003/login' -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop; Write-Host ('服务已就绪：HTTP ' + $r.StatusCode) } catch { Write-Host '仍在启动中，稍后刷新浏览器即可。' }"

echo.
echo  访问地址：http://127.0.0.1:8003
echo  停止服务：双击 stop.bat
echo.
pause
