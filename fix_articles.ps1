$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host '== 1/4 停止 Crawl4AI 容器 ==' -ForegroundColor Cyan
docker compose -f docker-compose.crawler.yml stop
Start-Sleep -Seconds 3

$backups = @(Get-ChildItem -LiteralPath (Join-Path $root 'data') -Filter 'crawler_articles.db.bak.*.db' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
if ($backups.Count -eq 0) {
    Write-Host '找不到备份文件，无法继续。' -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}
$backup = $backups[0].FullName
$db = Join-Path $root 'data\crawler_articles.db'
Write-Host "== 2/4 恢复备份: $backup ==" -ForegroundColor Cyan
Copy-Item -LiteralPath $backup -Destination $db -Force

Write-Host '== 3/4 迁移旧库中的文章 ==' -ForegroundColor Cyan
$pythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $pythonCmd = 'python' }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $pythonCmd = 'py' }
else {
    Write-Host '找不到 python，请先安装或激活 Python 环境。' -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}
& $pythonCmd (Join-Path $root 'tools\migrate_articles_from_legacy.py')
if ($LASTEXITCODE -ne 0) {
    Write-Host '文章迁移失败，请把上方错误信息发给我。' -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

Write-Host '== 4/4 启动容器 ==' -ForegroundColor Cyan
docker compose -f docker-compose.crawler.yml up -d
Write-Host '完成。请刷新 http://localhost:8004 查看文章。' -ForegroundColor Green
Read-Host '按回车关闭'
