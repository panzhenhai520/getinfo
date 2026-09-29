<#
  CollectInfo 启动脚本
  ====================
  默认 Docker 模式（推荐）：
      .\start-local.ps1
      .\start-local.ps1 -Build          # 强制重建镜像
  本地直跑模式（开发调试）：
      .\start-local.ps1 -Local
  停止 Docker 服务：
      .\start-local.ps1 -Stop
#>

param(
    [ValidateSet('Docker','Local')]
    [string]$Mode = 'Docker',
    [switch]$Build,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

function Write-Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

# 1) 生成 .env（仅当不存在时，避免覆盖已有配置）
if (-not (Test-Path -LiteralPath '.env')) {
    Write-Warn '.env 不存在，已从 .env.example 生成占位配置；请随后填写真实密钥。'
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
} else {
    Write-Step '.env 已存在，保留不动。'
}

# 2) 停止服务
if ($Stop) {
    if ($Mode -ne 'Docker') { Write-Err '-Stop 仅支持 Docker 模式。'; exit 1 }
    Write-Step '停止并移除 Docker 服务...'
    & docker compose -f docker-compose.crawler.yml down
    Write-Ok '已停止。'
    exit 0
}

# 3) Docker 模式（推荐）
if ($Mode -eq 'Docker') {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Err '未检测到 docker，请先安装 Docker Desktop 并切换到 Linux 容器。'
        exit 1
    }
    $dockerArgs = @('compose','-f','docker-compose.crawler.yml','up','-d')
    if ($Build) { $dockerArgs += '--build' }
    Write-Step "启动容器：docker $($dockerArgs -join ' ')  （首次构建耗时较长）"
    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) { Write-Err 'docker compose 启动失败。'; exit 1 }

    Write-Step '等待 Web 健康检查（最多约 2 分钟）...'
    $webUrl = 'http://localhost:8004/api/system/health'
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri $webUrl -TimeoutSec 3 -UseBasicParsing
            if ($resp.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }

    Write-Ok 'Web 服务已启动：'
    Write-Host '  页面：http://localhost:8004'
    Write-Host '  健康检查：http://localhost:8004/api/system/health'
    Write-Host '  远端 Pipeline 网关：http://10.88.0.1:11236'
    if (-not $healthy) { Write-Warn '健康检查尚未通过，可用 docker ps / docker compose logs 排查。' }
    exit 0
}

# 4) Local 模式（Windows 本地直跑）
if ($Mode -eq 'Local') {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Write-Err '未检测到 python。'; exit 1 }

    if (-not (Get-Command redis-server -ErrorAction SilentlyContinue)) {
        if (Get-Command docker -ErrorAction SilentlyContinue) {
            Write-Step '启动 Redis 容器（collectinfo-redis）...'
            & docker run -d --name collectinfo-redis -p 6379:6379 redis:7-alpine 2>$null
        } else {
            Write-Warn '未检测到 Redis 或 Docker，请自行准备 127.0.0.1:6379。'
        }
    }

    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        Write-Step '创建虚拟环境 .venv ...'
        & python -m venv .venv
    }

    Write-Step '安装依赖（首次较慢）...'
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Err '依赖安装失败。'; exit 1 }

    Write-Ok '准备启动 Flask（Ctrl+C 停止）...'
    $env:FLASK_HOST = '0.0.0.0'
    $env:FLASK_PORT = '8003'
    Write-Host '  页面：http://localhost:8003'
    & .\.venv\Scripts\python.exe start_with_schedule.py
    exit $LASTEXITCODE
}