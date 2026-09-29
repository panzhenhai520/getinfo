@echo off
echo ====================================
echo 重启爬虫服务
echo ====================================
echo.

echo 正在查找 Python 进程...
tasklist | findstr python.exe

echo.
echo 正在停止所有 Python 进程...
taskkill /F /IM python.exe /T

echo.
echo 等待 3 秒...
timeout /t 3 /nobreak

echo.
echo 正在启动服务...
start "FireCrawl Service" python firecrawl_app.py

echo.
echo ====================================
echo 服务已重启！
echo ====================================
echo.
echo 请等待 5-10 秒让服务完全启动
echo 然后访问: http://localhost:8003
echo.
pause

