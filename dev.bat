@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d F:\CollectInfo

echo [1/2] 启动 Web (http://127.0.0.1:8003) ...
start "CollectInfo-Web" cmd /k python start_with_schedule.py

echo [2/2] 启动情报 Worker (负责候选爬取派发) ...
start "CollectInfo-Worker" cmd /k python intel_worker.py --job-type candidate_dispatch --no-periodic-scheduler

echo.
echo 两个进程已分别在独立窗口启动。
echo 停止方式：到对应窗口按 Ctrl+C，或直接关闭窗口。
