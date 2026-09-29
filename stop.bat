@echo off
chcp 65001 >nul
title 停止情报系统（8003）

echo ============================================================
echo  停止 8003 服务（结束爬虫调度 + Flask 进程）
echo ============================================================

echo 结束匹配 start_with_schedule / run_flask 的 python 进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'start_with_schedule|run_flask' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  已停止 PID ' $_.ProcessId }"

echo 兜底：结束监听 8003 端口的进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8003 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host '  已停止 PID ' $_.OwningProcess ' (端口8003)' }"

timeout /t 2 >nul
echo.
echo  已停止 8003 服务。
echo.
pause
