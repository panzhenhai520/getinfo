<#
.SYNOPSIS
  本机(F:\CollectInfo\remote_pipeline) → VPN 流水线主机(10.88.0.1) 部署。

.DESCRIPTION
  把 remote_pipeline 源码上传到 VPN 主机的 /home/panython/CollectInfo/remote_pipeline，
  备份旧 app.py 后执行 deploy.sh（docker compose up -d --build）。
  只重建 collectinfo-pipeline 容器，不触碰 voice-project/Ollama/CosyVoice/RAGFlow。
  生产 .env 不被覆盖（本机没有 .env，也不会传 .env.example 覆盖现场配置）。

.EXAMPLE
  pwsh -File F:\CollectInfo\deploy_vpn_pipeline.ps1
#>
[CmdletBinding()]
param(
  [string]$VpnHost    = 'root@10.88.0.1',
  [string]$RemoteDir  = '/home/panython/CollectInfo/remote_pipeline',
  [int]   $HealthWait = 120
)

$ErrorActionPreference = 'Stop'
$RepoRoot = 'F:\CollectInfo'
$AskPass  = Join-Path $RepoRoot '_askpass_vpn.cmd'
$Ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
$Scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$Staging = Join-Path $env:TEMP ('vpndeploy_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))

if (-not (Test-Path $AskPass)) { throw "缺少 $AskPass（VPN SSH 免交互密码脚本）" }
$env:SSH_ASKPASS        = $AskPass
$env:DISPLAY            = 'localhost:0'
$env:SSH_ASKPASS_REQUIRE = 'force'

function Invoke-Vpn([string]$Command) {
  for ($attempt = 1; $attempt -le 4; $attempt++) {
    & $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 $VpnHost $Command
    if ($LASTEXITCODE -eq 0) { return }
    if ($attempt -eq 4) { throw "远程命令失败(exit $LASTEXITCODE): $Command" }
    Write-Host ("    远程命令失败，重试 {0}/4（{1} 秒后）" -f $attempt, (5 * $attempt))
    Start-Sleep -Seconds (5 * $attempt)
  }
}

New-Item -ItemType Directory -Path $Staging -Force | Out-Null

# ── 1) 打包 remote_pipeline 源码 ─────────────────────────────
Write-Host '=== [1/3] 打包 remote_pipeline 源码 ===' -ForegroundColor Cyan
$tarball = Join-Path $Staging 'remote_pipeline.tar.gz'
Push-Location (Join-Path $RepoRoot 'remote_pipeline')
try {
  & tar -czf $tarball --exclude=./__pycache__ --exclude=./.env --exclude='./data' .
  if ($LASTEXITCODE -ne 0) { throw 'tar 打包失败' }
} finally { Pop-Location }
$mb = [math]::Round((Get-Item $tarball).Length / 1KB, 1)
Write-Host "    源码包: $mb KB"

# ── 2) 上传 + 备份 + 解包 + 重建 ─────────────────────────────
Write-Host '=== [2/3] 上传到 VPN 主机并重建 ===' -ForegroundColor Cyan
& $Scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $tarball "${VpnHost}:/tmp/remote_pipeline.tar.gz"
if ($LASTEXITCODE -ne 0) { throw 'scp 上传失败' }
Invoke-Vpn ("mkdir -p `"$RemoteDir`" && " +
  "cp -f `"$RemoteDir/app.py`" `"$RemoteDir/app.py.bak_release_$(Get-Date -Format 'yyyyMMdd_HHmmss')`" 2>/dev/null || true && " +
  "tar -xzf /tmp/remote_pipeline.tar.gz -C `"$RemoteDir`" && " +
  "sed -i 's/\r`$//' `"$RemoteDir/deploy.sh`" && chmod +x `"$RemoteDir/deploy.sh`" && " +
  "cd `"$RemoteDir`" && ./deploy.sh")

# ── 3) 健康检查 + 提示词验证 ────────────────────────────────
Write-Host '=== [3/3] 健康检查 + 强化提示词验证 ===' -ForegroundColor Cyan
$ok = $false
for ($i = 1; $i -le [math]::Ceiling($HealthWait / 5); $i++) {
  Start-Sleep -Seconds 5
  & $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $VpnHost `
      "curl -fsS -m 5 http://127.0.0.1:11236/v1/health -o /dev/null" 2>$null
  if ($LASTEXITCODE -eq 0) { $ok = $true; break }
}
if (-not $ok) { throw "VPN 健康检查超时（$HealthWait 秒），请查看: docker logs --tail 60 collectinfo-pipeline" }
Write-Host '    ✅ collectinfo-pipeline 健康'
& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $VpnHost `
    "docker exec collectinfo-pipeline grep -c 'OCR 错别字' /app/remote_pipeline/app.py" | ForEach-Object { Write-Host ("    强化提示词标记(应为≥1): " + $_) }

Write-Host '=== VPN 部署完成 ✅ ===' -ForegroundColor Green
Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
