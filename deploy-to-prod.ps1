<#
.SYNOPSIS
  本机(F:\CollectInfo) → 生产机(117.50.211.93) 一键部署。

.DESCRIPTION
  生产机的源码是烘进镜像里的（/www/CollectInfo_latest_new 只放 compose/.env/数据卷，没有源码），
  所以线上代码只能靠镜像分发。全量 docker save 出来的 tar 有 ~3.9GB，每次都传太慢。
  本脚本改为：只把「源码」打成 ~15MB 的 tar 上传，在生产机上用 overlay Dockerfile
  （FROM 当前镜像 + COPY 源码）`docker build` 出同名新镜像，再 `docker compose up -d` 重建服务。
  好处：传输小、构建可复现、web/worker/intel-worker 三个容器共用同一镜像，一起更新。

  原则：**只在本机改代码**。所有改动先落到 F:\CollectInfo，再执行本脚本推送到生产机；
  不要直接登生产机改文件。

.EXAMPLE
  pwsh -File F:\CollectInfo\deploy-to-prod.ps1
#>
[CmdletBinding()]
param(
  [string]$ProdHost   = 'root@10.88.0.3',
  [string]$RemoteDir  = '/www/CollectInfo_latest_new',
  [string]$Image      = 'collectinfo-web:latest',
  [int]   $HealthWait = 180,
  [switch]$SkipComposeSync     # 不同步 docker-compose.prod.yml
)

$ErrorActionPreference = 'Stop'
$RepoRoot = 'F:\CollectInfo'
$AskPass  = Join-Path $RepoRoot '_askpass.cmd'
$Ssh  = 'C:\Windows\System32\OpenSSH\ssh.exe'
$Scp  = 'C:\Windows\System32\OpenSSH\scp.exe'
$Staging = Join-Path $env:TEMP ('cideploy_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))

# 生产机上源码的落地位置（容器内 /app）
# 排除：数据卷、密钥、缓存、打包产物——这些绝不能进镜像
$Excludes = @(
  '--exclude=./data', '--exclude=./deploy-data', '--exclude=./.git', '--exclude=./__pycache__',
  '--exclude=./crawl_results', '--exclude=./crawl_logs', '--exclude=./auth_storage',
  '--exclude=./.postgres_deps', '--exclude=./industry_pack_backups', '--exclude=./vendor',
  '--exclude=./node_modules', '--exclude=./.venv', '--exclude=./venv',
  '--exclude=./.env', '--exclude=./.env.*', '--exclude=./*.tar', '--exclude=./*.tar.gz',
  '--exclude=./.tmp-*', '--exclude=./*.zip', '--exclude=./*.tar.*', '--exclude=./*.tgz',
  '--exclude=./*.db', '--exclude=./*.log'
)
# 仓库根目录下的本机临时件（_server_main.py、_tmp_*.py、截图/dump、_askpass*.cmd…）不进镜像。
# 但它们不能写进上面的 $Excludes：Windows 自带 bsdtar 3.5.2 的 --exclude 既匹配完整路径、
# 也匹配文件名，`--exclude=./_*` 会把 templates/_dashboard_nav.html 这类子目录文件一起排掉
# （实测；漏掉它会让所有页面 include 失败）。所以根目录改为「显式枚举要打包的顶层条目」。
$RootSkipNames = @(
  '.git', '__pycache__', 'data', 'deploy-data', 'crawl_results', 'crawl_logs', 'auth_storage',
  '.postgres_deps', 'industry_pack_backups', 'vendor', 'node_modules', '.venv', 'venv'
)
$RootSkipSuffixes = @('.tar', '.tar.gz', '.tgz', '.zip', '.db', '.log')
$RootEntries = @()
foreach ($item in (Get-ChildItem -Force -LiteralPath $RepoRoot)) {
  $name = $item.Name
  if ($name.StartsWith('_')) { continue }
  if ($name.StartsWith('.tmp-')) { continue }
  if ($name -eq '.env' -or $name.StartsWith('.env.')) { continue }
  if ($RootSkipNames -contains $name) { continue }
  if (@($RootSkipSuffixes | Where-Object { $name.EndsWith($_) }).Count -gt 0) { continue }
  $RootEntries += './' + $name
}
if (-not $RootEntries) { throw '打包条目为空，已中止' }

function Set-SshAuth {
  if (-not (Test-Path $AskPass)) { throw "缺少 $AskPass（SSH 免交互密码脚本）" }
  $env:SSH_ASKPASS        = $AskPass
  $env:DISPLAY            = 'localhost:0'
  $env:SSH_ASKPASS_REQUIRE = 'force'
}
function Invoke-Ssh([string]$Command) {
  # 生产 sshd 会间歇性拒连（kex_exchange_identification: Connection closed by remote host）。
  # 一次失败就整体中断，会把已经传完的源码包和白做的构建全废掉，所以这里重试并退避。
  for ($attempt = 1; $attempt -le 4; $attempt++) {
    & $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 $ProdHost $Command
    if ($LASTEXITCODE -eq 0) { return }
    if ($attempt -eq 4) { throw "远程命令失败(exit $LASTEXITCODE): $Command" }
    Write-Host ("    远程命令失败，重试 {0}/4（{1} 秒后）" -f $attempt, (5 * $attempt))
    Start-Sleep -Seconds (5 * $attempt)
  }
}
# 断点续传上传。scp 不支持续传：链路慢时（实测过 ~10 KB/s，5MB 要 ~500 秒）
# 一旦被超时打断就只剩一个残包，重跑还得从头再传。这里改为 ssh 标准输入分片追加：
# 每片传完核对远端字节数，中断后重跑自动从已收到的字节数继续。
function Send-FileChunks([string]$LocalPath, [string]$RemotePath, [int]$ChunkBytes = 1048576) {
  $size = (Get-Item $LocalPath).Length
  $raw = (& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost "stat -c %s '$RemotePath' 2>/dev/null || echo 0") | Select-Object -Last 1
  $have = if ($raw -match '^\d+$') { [int64]$raw } else { 0 }
  if ($have -gt $size) { Invoke-Ssh "rm -f '$RemotePath'" | Out-Null; $have = 0 }
  # 续传前提：远端已有的字节必须是本次本地文件的前缀。源码包每次打包内容都会变，
  # 若不远校验就追加，会把新包拼到旧残包后面，得到一个大小对得上、内容却损坏的 tar。
  if ($have -gt 0) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $prefix = New-Object byte[] $have
    $probe = [System.IO.File]::OpenRead($LocalPath)
    try { $probe.Read($prefix, 0, [int]$have) | Out-Null } finally { $probe.Dispose() }
    $localHash = (($sha.ComputeHash($prefix) | ForEach-Object { $_.ToString('x2') }) -join '')
    $rawHash = (& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost "head -c $have '$RemotePath' | sha256sum | cut -d' ' -f1") | Select-Object -Last 1
    $remoteHash = if ($rawHash -match '^[0-9a-f]{64}$') { $rawHash.Trim() } else { '' }
    if ($remoteHash -ne $localHash) {
      Write-Host '    远端残包与本次源码包不一致，丢弃后重新上传'
      Invoke-Ssh "rm -f '$RemotePath'" | Out-Null
      $have = 0
    }
  }
  if ($have -eq $size) { Write-Host ("    已完整（{0} 字节），跳过上传" -f $size); return }
  if ($have -gt 0) { Write-Host ("    断点续传：从 {0}/{1} 字节继续" -f $have, $size) }
  $chunkPath = Join-Path $Staging 'chunk.bin'
  $resets = 0
  $source = [System.IO.File]::OpenRead($LocalPath)
  try {
    $source.Seek($have, [System.IO.SeekOrigin]::Begin) | Out-Null
    $buffer = New-Object byte[] 65536
    while ($have -lt $size) {
      $remaining = [Math]::Min([int64]$ChunkBytes, $size - $have)
      $out = [System.IO.File]::Create($chunkPath)
      try {
        while ($remaining -gt 0) {
          $read = $source.Read($buffer, 0, [int][Math]::Min([int64]$buffer.Length, $remaining))
          if ($read -le 0) { break }
          $out.Write($buffer, 0, $read)
          $remaining -= $read
        }
      } finally { $out.Dispose() }
      # 追加与回读放在同一条 SSH 连接里完成。分开成两条连接时，回读到的字节数
      # 可能是上一次追加尚未落盘时的旧值，据此切片就会写过界（实测把远端写成 109.9%）。
      $now = $have
      for ($attempt = 1; $attempt -le 3; $attempt++) {
        $cmd = '"' + $Ssh + '" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ' + $ProdHost + ' "cat >> ''' + $RemotePath + '''; stat -c %s ''' + $RemotePath + '''" < "' + $chunkPath + '"'
        $rawOut = (cmd /c $cmd) 2>$null
        $sizeLine = ($rawOut | Where-Object { $_ -match '^\d+$' } | Select-Object -Last 1)
        if ($sizeLine) { $now = [int64]$sizeLine; break }
        Write-Host ("    本片回读失败，重试 {0}/3" -f $attempt)
        Start-Sleep -Seconds 5
      }
      if ($now -gt $size) {
        # 远端比本地还大 → 之前某次追加写入了多余字节，整份已不可信，删除重传
        $resets += 1
        if ($resets -gt 2) { throw ("远端文件反复超出本地大小，请检查 '$RemotePath' 后重跑") }
        Write-Host ("    远端 {0} > 本地 {1} 字节，残包损坏，重新上传" -f $now, $size)
        Invoke-Ssh "rm -f '$RemotePath'" | Out-Null
        $source.Seek(0, [System.IO.SeekOrigin]::Begin) | Out-Null
        $have = 0
        continue
      }
      if ($now -le $have) { throw ("上传无进展（远端停留 {0} 字节），链路可能已断，重跑本脚本可续传" -f $now) }
      $have = $now
      Write-Host ("    {0}/{1} 字节（{2}%）" -f $have, $size, [Math]::Round(100.0 * $have / $size, 1))
    }
  } finally { $source.Dispose() }
  if ($have -ne $size) { throw ("上传不完整：本地 {0} 字节，远端 {1} 字节" -f $size, $have) }
  Write-Host ("    ✅ 上传完整（{0} 字节）" -f $size)
}

# 上传后必须整包校验：链路抖动时会出现「远端字节数与本地完全一致、内容却已损坏」
# （实测 tar 解包报 gzip: invalid compressed data--format violated），而分片回读只核对
# 字节数，于是下次重跑会信以为真地跳过上传，把坏包直接送进构建。这里补一次全量 sha256
# 比对，不一致就删掉重传（最多 3 次），把「静默坏包」挡在构建之前。
function Send-FileResumable([string]$LocalPath, [string]$RemotePath, [int]$ChunkBytes = 1048576) {
  $localHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $LocalPath).Hash.ToLower()
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    Send-FileChunks $LocalPath $RemotePath $ChunkBytes
    $raw = (& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost "sha256sum '$RemotePath' | cut -d' ' -f1") | Select-Object -Last 1
    $remoteHash = if ($raw -match '^[0-9a-f]{64}$') { $raw.Trim() } else { '' }
    if ($remoteHash -eq $localHash) {
      Write-Host '    ✅ 整包 sha256 校验通过'
      return
    }
    $shown = if ($remoteHash) { $remoteHash.Substring(0, 12) } else { '读取失败' }
    Write-Host ("    远端整包哈希不一致（{0} vs {1}），丢弃后重传（第 {2}/3 次）" -f $shown, $localHash.Substring(0, 12), $attempt)
    Invoke-Ssh "rm -f '$RemotePath'" | Out-Null
  }
  throw ("上传校验失败（连续 3 次哈希不一致）：{0}" -f $RemotePath)
}

Set-SshAuth
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

# ── 1) 打包源码 ────────────────────────────────────────────────
Write-Host '=== [1/5] 打包本机源码（排除数据卷/密钥） ===' -ForegroundColor Cyan
$tarball = Join-Path $Staging 'src.tar.gz'
Push-Location $RepoRoot
try {
  & tar -czf $tarball @Excludes @RootEntries
  if ($LASTEXITCODE -ne 0) { throw 'tar 打包失败' }
} finally { Pop-Location }
$mb = [math]::Round((Get-Item $tarball).Length / 1MB, 2)
Write-Host "    源码包: $mb MB"

# ── 2) 生成远程脚本（overlay 构建 + 重建服务） ─────────────────
# 注意：用 LF 换行写入，避免 bash 解析 CRLF 出错
$remote = @'
set -euo pipefail
IMG="__IMAGE__"
REMOTE_DIR="__REMOTE_DIR__"
CTX=/tmp/collectinfo-deploy/buildctx

echo "=== [3/5] 解包到构建上下文 ==="
rm -rf "$CTX"; mkdir -p "$CTX"
tar -xzf /tmp/collectinfo-deploy/src.tar.gz -C "$CTX"

echo "=== [4/5] 构建新镜像（层数超限则全量重建） ==="
# 保留一份上一版镜像，便于回滚：docker tag collectinfo-web:rollback ...
if docker image inspect "$IMG" >/dev/null 2>&1; then
  docker tag "$IMG" "${IMG%%:*}:rollback" 2>/dev/null || true
fi
# overlay 每次部署叠 2 层（COPY + RUN），累计到 Docker 上限后再也构不出来，报错是
# "max depth exceeded"。层数接近上限时改用「压平重建」：把现有镜像 export→import
# 成单层基底，再在上面叠新代码（不依赖仓库 Dockerfile 的网络安装步骤，
# 已实测仓库全量重建在生产网络下会因 gh/apt/pip 下载失败而中断）。
LAYERS=$(docker image inspect "$IMG" --format '{{len .RootFS.Layers}}' 2>/dev/null || echo 0)
if [ "$LAYERS" -ge 118 ]; then
  echo "    当前镜像 $LAYERS 层已达上限区间 → 压平镜像重置层数"
  docker rm -f flat-src >/dev/null 2>&1 || true
  docker create --name flat-src "$IMG"
  docker export flat-src | docker import - "${IMG%%:*}:flat"
  docker rm flat-src >/dev/null
  docker inspect "$IMG" --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/old-env.txt
  {
    echo "FROM ${IMG%%:*}:flat"
    awk 'NF {eq=index($0,"="); print "ENV " substr($0,1,eq-1) "=\"" substr($0,eq+1) "\""}' /tmp/old-env.txt
    echo 'WORKDIR /app'
    echo 'ENTRYPOINT ["/app/docker-entrypoint.sh"]'
    echo 'CMD ["gunicorn","-w","2","--threads","4","--bind","0.0.0.0:8003","--timeout","300","firecrawl_app:app"]'
    echo 'COPY . /app/'
    echo 'RUN sed -i "s/\r$//" /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh'
  } > /tmp/collectinfo-deploy/Dockerfile.flat
  if docker build -q -t "$IMG" -f /tmp/collectinfo-deploy/Dockerfile.flat "$CTX" | tail -1; then
    echo "    压平重建通过，层数已重置为 $(docker image inspect "$IMG" --format '{{len .RootFS.Layers}}')"
  else
    echo "    ❌ 压平重建失败，保持原镜像不变"; exit 1
  fi
else
  cat > /tmp/collectinfo-deploy/Dockerfile.deploy <<'DOCKER'
FROM collectinfo-web:latest
COPY . /app/
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh
DOCKER
  if ! docker build -q -t "$IMG" -f /tmp/collectinfo-deploy/Dockerfile.deploy "$CTX" | tail -1; then
    echo "    overlay 构建失败 → 回退到仓库 Dockerfile 全量重建"
    docker build -q -t "$IMG" -f "$CTX/Dockerfile" "$CTX" | tail -1
  fi
fi
echo "    新镜像: $(docker image inspect "$IMG" --format '{{.Id}}' | cut -c1-19)"

if [ "__SKIP_COMPOSE__" = "0" ] && [ -f "$CTX/docker-compose.prod.yml" ]; then
  cp "$CTX/docker-compose.prod.yml" "$REMOTE_DIR/docker-compose.prod.yml"
  echo "    已同步 docker-compose.prod.yml"
fi

echo "=== [4.5/5] 安装新依赖（pip + patchright 浏览器，已存在则跳过） ==="
if docker run --rm --entrypoint python3 "$IMG" -c "import markitdown, autoscraper, scrapling.fetchers, qrcode, markdownify" >/dev/null 2>&1; then
  echo "    新依赖已存在，跳过依赖层构建"
else
  cat > /tmp/collectinfo-deploy/Dockerfile.deps <<'DOCKER'
FROM collectinfo-web:latest
RUN python3 -m pip install --no-cache-dir \
      "qrcode>=8.0" "markdownify>=0.13.1" "markitdown[pdf,docx,pptx,xlsx]>=0.1.7" \
      "autoscraper>=1.1.14" "curl_cffi>=0.16.3" "scrapling>=0.4.15" "msgspec>=0.19" "patchright>=1.62.3" \
 && python3 -m patchright install chromium
DOCKER
  if docker build -q -t "$IMG" -f /tmp/collectinfo-deploy/Dockerfile.deps /tmp/collectinfo-deploy | tail -1; then
    echo "    依赖层构建通过"
  else
    echo "    ❌ 依赖层构建失败，保持上一版镜像，中断部署"; exit 1
  fi
fi

echo "=== [5/5] 重建全部服务（web + worker + intel-worker） ==="
cd "$REMOTE_DIR"
docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -12
'@
$remote = $remote.Replace('__IMAGE__', $Image).Replace('__REMOTE_DIR__', $RemoteDir).
                  Replace('__SKIP_COMPOSE__', $(if ($SkipComposeSync) { '1' } else { '0' }))
$remotePath = Join-Path $Staging 'deploy-remote.sh'
[System.IO.File]::WriteAllText($remotePath, $remote, (New-Object System.Text.UTF8Encoding($false)))

# ── 3) 上传 ───────────────────────────────────────────────────
Write-Host '=== [2/5] 上传到生产机（断点续传） ===' -ForegroundColor Cyan
Invoke-Ssh 'mkdir -p /tmp/collectinfo-deploy' | Out-Null
Send-FileResumable $tarball '/tmp/collectinfo-deploy/src.tar.gz'
Send-FileResumable $remotePath '/tmp/collectinfo-deploy/deploy-remote.sh'

# ── 4) 远程构建 + 重建 ────────────────────────────────────────
Invoke-Ssh 'bash /tmp/collectinfo-deploy/deploy-remote.sh'

# ── 5) 健康检查 ───────────────────────────────────────────────
Write-Host '=== 等待 web 健康 ===' -ForegroundColor Cyan
$ok = $false
for ($i = 1; $i -le [math]::Ceiling($HealthWait / 5); $i++) {
  Start-Sleep -Seconds 5
  & $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost `
      "curl -fsS -o /dev/null http://127.0.0.1:8003/api/system/health" 2>$null
  if ($LASTEXITCODE -eq 0) { $ok = $true; break }
}
if (-not $ok) { throw "健康检查超时（$HealthWait 秒），请查看: docker logs --tail 50 collectinfo-web" }

& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost `
    'docker ps --format table | head -6'
# 清掉本次构建淘汰下来的无引用旧镜像层，避免根分区被逐次构建撑满
# （只删无容器引用的 dangling 镜像；rollback 标签指向同一批层，不占额外空间）
& $Ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $ProdHost `
    'docker image prune -f >/dev/null 2>&1 || true; df -h / | tail -1'

# ── 6) 一致性核对：用源码指纹确认生产跑的确实是本机这份代码 ────────────
# 指纹算法在 tools/source_fingerprint.py（与 $Excludes 同源），生产端由
# /api/system/version 现场计算，两边数字相同才算代码一致。
Write-Host '=== 一致性核对（逐文件比对） ===' -ForegroundColor Cyan
# 只比指纹没用：两边文件集合一有差（容器里有历史产物、本机有测试缓存）就报警，
# 既不告诉你差在哪、也无法判断是不是真的代码不同步。改为逐文件比对，直接给出
# 「内容不同 / 容器多出 / 缺少」的数量与文件名——内容不同为 0 才算代码一致。
try {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py -and (Test-Path 'C:\Anaconda\python.exe')) { $py = 'C:\Anaconda\python.exe' }
  if (-not $py) { throw '未找到 python，跳过核对' }
  $mapPath = Join-Path $Staging 'local_fp.json'
  & $py -c "import json,sys; from pathlib import Path; sys.path.insert(0,r'$RepoRoot'); from tools.source_fingerprint import file_hashes; json.dump(file_hashes(Path(r'$RepoRoot')), open(r'$mapPath','w',encoding='utf-8'))" | Out-Null
  if (-not (Test-Path $mapPath)) { throw '本机哈希表生成失败' }
  Send-FileResumable $mapPath '/tmp/collectinfo-deploy/local_fp.json'
  # 核对脚本写成文件再上传：ssh 命令行多层引号转义是把 python -c 拆坏的根源
  $checkPy = @'
import json
import sys
from pathlib import Path
sys.path.insert(0, "/app")
from tools.source_fingerprint import file_hashes
local = json.load(open("/tmp/local_fp.json", encoding="utf-8"))
remote = file_hashes(Path("/app"))
extra = sorted(set(remote) - set(local))
missing = sorted(set(local) - set(remote))
changed = sorted(p for p in set(local) & set(remote) if local[p] != remote[p])
print("LOCAL=%d REMOTE=%d" % (len(local), len(remote)))
print("CHANGED=%d EXTRA=%d MISSING=%d" % (len(changed), len(extra), len(missing)))
for p in changed[:8]: print("   ~ " + p)
for p in extra[:8]: print("   + " + p)
for p in missing[:8]: print("   - " + p)
'@
  $checkPath = Join-Path $Staging 'fp_check.py'
  [System.IO.File]::WriteAllText($checkPath, $checkPy, (New-Object System.Text.UTF8Encoding($false)))
  Send-FileResumable $checkPath '/tmp/collectinfo-deploy/fp_check.py'
  Invoke-Ssh 'docker cp /tmp/collectinfo-deploy/local_fp.json collectinfo-web:/tmp/local_fp.json >/dev/null 2>&1; docker cp /tmp/collectinfo-deploy/fp_check.py collectinfo-web:/tmp/fp_check.py >/dev/null 2>&1' | Out-Null
  $diff = Invoke-Ssh 'docker exec collectinfo-web python3 /tmp/fp_check.py'
  $diff | ForEach-Object { Write-Host ("    " + $_) }
  if ($diff -match 'CHANGED=0 ') {
    Write-Host '    ✅ 共同文件内容完全一致（多出/缺少的都是构建产物或本机缓存）' -ForegroundColor Green
  } else {
    Write-Host '    ⚠️ 存在内容不同的文件，见上方 ~ 行' -ForegroundColor Yellow
  }
} catch {
  Write-Host ("    核对未完成：{0}" -f $_.Exception.Message) -ForegroundColor Yellow
}
Write-Host '=== 部署完成 ✅ ===' -ForegroundColor Green
Write-Host '  回滚: docker tag collectinfo-web:rollback collectinfo-web:latest && cd /www/CollectInfo_latest_new && docker compose -f docker-compose.prod.yml up -d'
Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
