# 数据导出脚本：在旧服务器（当前 Windows 机器）运行，导出 PostgreSQL 全库 + 文件数据
$ErrorActionPreference = "Stop"
$PGDUMP = "C:\Program Files\PostgreSQL\15\bin\pg_dump.exe"
$env:PGPASSWORD = "postgres"    # 与 .env 的 POSTGRES_PASSWORD 一致
$out = "deploy-data"
New-Item -ItemType Directory -Force -Path $out | Out-Null

# 1) PostgreSQL 全量导出（自定义压缩格式，含全部 101 张表）
Write-Host "=== 导出 PostgreSQL collectinfo ==="
& $PGDUMP -h 127.0.0.1 -p 5432 -U postgres -d collectinfo -Fc -f "$out\collectinfo.dump"
if ($LASTEXITCODE -ne 0) { throw "pg_dump 失败" }

# 2) 文件数据打包（爬取结果/登录态/上传logo/数据目录：报告、TTS缓存、SQLite备份等）
Write-Host "=== 打包文件数据 ==="
tar -czf "$out\file-data.tar.gz" crawl_results auth_storage static/uploads data

Write-Host ""
Write-Host "导出完成："
Write-Host "  $out\collectinfo.dump   （PostgreSQL 全库）"
Write-Host "  $out\file-data.tar.gz   （文件数据）"
