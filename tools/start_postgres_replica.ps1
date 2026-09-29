# 启动 PostgreSQL 副本并全量迁移当前 SQLite 数据
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host '[1/4] 启动 PostgreSQL (postgres:16, 本机端口 5433)'
docker compose -f docker-compose.postgres.yml up -d postgres

Write-Host '[2/4] 等待 PostgreSQL 就绪'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    docker compose -f docker-compose.postgres.yml exec -T postgres pg_isready -U collectinfo -d collectinfo *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw 'PostgreSQL 未就绪' }

Write-Host '[3/4] 全量迁移 SQLite -> PostgreSQL (重建目标表)'
python tools\migrate_sqlite_to_postgres.py --sqlite data\crawler_articles.db --drop

Write-Host '[4/4] 校验表数量和行内容哈希'
python tools\migrate_sqlite_to_postgres.py --sqlite data\crawler_articles.db --verify
