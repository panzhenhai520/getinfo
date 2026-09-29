$ErrorActionPreference = "Stop"

$SourceDir = "D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013"
$PackageRoot = "D:\docker-data\ragflow\ragflow-v0.25.6-offline-package-8013-clean"
$DeployDir = Join-Path $PackageRoot "ragflow-v0.25.6-offline"
$ImageDir = Join-Path $PackageRoot "images"
$LogPath = Join-Path (Split-Path -Parent $PackageRoot) "package-ragflow-v0256-offline-8013.log"
$RagflowContainer = "ragflow-v0256-fresh-8013-ragflow-cpu-1"
$RagflowRootfsTar = Join-Path $ImageDir "infiniflow_ragflow_v0.25.6.rootfs.tar"

$Images = @(
  "registry.cn-hangzhou.aliyuncs.com/mybase-tools/redis:latest",
  "quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z",
  "mysql:8.0.39",
  "elasticsearch:8.11.3"
)

function Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
  Write-Host $line
}

function Run-Native {
  param(
    [string]$WorkingDirectory,
    [string]$Exe,
    [string[]]$CommandArgs,
    [int]$TimeoutSeconds = 1800
  )

  Log ("RUN {0} {1} in {2}" -f $Exe, ($CommandArgs -join " "), $WorkingDirectory)
  Push-Location $WorkingDirectory
  $previousErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & $Exe @CommandArgs 2>&1 |
      ForEach-Object { $_.ToString() } |
      Tee-Object -FilePath $LogPath -Append
    $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
  }

  Log ("EXIT {0}" -f $code)
  if ($code -ne 0) {
    throw "Command failed with exit code ${code}: $Exe $($CommandArgs -join ' ')"
  }
}

function Run-CmdRedirect {
  param(
    [string]$WorkingDirectory,
    [string]$CommandLine,
    [int]$TimeoutSeconds = 7200
  )

  Log ("RUN cmd.exe /c {0} in {1}" -f $CommandLine, $WorkingDirectory)
  Push-Location $WorkingDirectory
  try {
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $CommandLine) -Wait -PassThru -WindowStyle Hidden
    $code = $process.ExitCode
  } finally {
    Pop-Location
  }

  Log ("EXIT {0}" -f $code)
  if ($code -ne 0) {
    throw "Command failed with exit code ${code}: cmd.exe /c $CommandLine"
  }
}

function Assert-InsidePackage {
  param([string]$Path)
  $rootFull = [System.IO.Path]::GetFullPath($PackageRoot).TrimEnd('\') + '\'
  $targetFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
  if (-not $targetFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write outside package root: $Path"
  }
}

function Write-LfFile {
  param(
    [string]$Path,
    [string[]]$Lines
  )

  Assert-InsidePackage $Path
  $text = ($Lines -join "`n") + "`n"
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [System.IO.File]::WriteAllText($Path, $text, $utf8NoBom)
}

function Copy-DeploymentSkeleton {
  if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "Missing source deployment: $SourceDir"
  }

  New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
  New-Item -ItemType Directory -Force -Path $DeployDir | Out-Null

  Log "Copying clean official deployment files without runtime data."
  & robocopy $SourceDir $DeployDir /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:16 /NP `
    /XD mysql_data minio_data esdata01 osdata01 infinity_data oceanbase ragflow-logs redis_data custom_server custom_server.disabled-* backend-image codex-backups app `
    /XF *.log *.runner.log remote-echo-test.txt FRESH-8013-NOTES.txt 2>&1 |
    Tee-Object -FilePath $LogPath -Append
  $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  Log ("ROBOCOPY EXIT {0}" -f $code)
  if ($code -ge 8) {
    throw "Robocopy failed with exit code ${code}"
  }

  foreach ($dir in @("mysql_data", "minio_data", "esdata01", "ragflow-logs", "redis_data")) {
    $target = Join-Path $DeployDir $dir
    Assert-InsidePackage $target
    New-Item -ItemType Directory -Force -Path $target | Out-Null
  }

  $sourceRedisConf = Join-Path $SourceDir "redis_data\redis.conf"
  $targetRedisConf = Join-Path $DeployDir "redis_data\redis.conf"
  if (Test-Path -LiteralPath $sourceRedisConf) {
    Copy-Item -LiteralPath $sourceRedisConf -Destination $targetRedisConf -Force
  } else {
    Write-LfFile -Path $targetRedisConf -Lines @(
      "protected-mode yes",
      "port 6379",
      "dir ./",
      "requirepass infini_rag_flow"
    )
  }
}

function Set-EnvValue {
  param(
    [string]$EnvPath,
    [string]$Key,
    [string]$Value
  )

  $lines = [System.Collections.Generic.List[string]]::new()
  foreach ($line in (Get-Content -LiteralPath $EnvPath)) {
    $lines.Add($line)
  }

  $pattern = "^\s*{0}\s*=" -f [regex]::Escape($Key)
  $replacement = "{0}={1}" -f $Key, $Value
  $found = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match $pattern) {
      $lines[$i] = $replacement
      $found = $true
    }
  }
  if (-not $found) {
    $lines.Add($replacement)
  }

  Set-Content -LiteralPath $EnvPath -Value $lines -Encoding UTF8
}

function Normalize-Env {
  $envPath = Join-Path $DeployDir ".env"
  if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing .env in deployment package."
  }

  Set-EnvValue -EnvPath $envPath -Key "COMPOSE_PROJECT_NAME" -Value "ragflow-v0256-offline-8013"
  Set-EnvValue -EnvPath $envPath -Key "DOC_ENGINE" -Value "elasticsearch"
  Set-EnvValue -EnvPath $envPath -Key "DEVICE" -Value "cpu"
  Set-EnvValue -EnvPath $envPath -Key "COMPOSE_PROFILES" -Value "elasticsearch,cpu"
  Set-EnvValue -EnvPath $envPath -Key "RAGFLOW_IMAGE" -Value "infiniflow/ragflow:v0.25.6"
  Set-EnvValue -EnvPath $envPath -Key "SVR_WEB_HTTP_PORT" -Value "8013"
  Set-EnvValue -EnvPath $envPath -Key "SVR_HTTP_PORT" -Value "29380"
  Set-EnvValue -EnvPath $envPath -Key "ADMIN_SVR_HTTP_PORT" -Value "29381"
  Set-EnvValue -EnvPath $envPath -Key "SVR_MCP_PORT" -Value "29382"
  Set-EnvValue -EnvPath $envPath -Key "GO_HTTP_PORT" -Value "29384"
  Set-EnvValue -EnvPath $envPath -Key "GO_ADMIN_PORT" -Value "29383"
  Set-EnvValue -EnvPath $envPath -Key "EXPOSE_MYSQL_PORT" -Value "5457"
  Set-EnvValue -EnvPath $envPath -Key "MINIO_PORT" -Value "9012"
  Set-EnvValue -EnvPath $envPath -Key "MINIO_CONSOLE_PORT" -Value "9013"
  Set-EnvValue -EnvPath $envPath -Key "REDIS_PORT" -Value "6382"
  Set-EnvValue -EnvPath $envPath -Key "ES_PORT" -Value "12102"
}

function Write-OfflineHelpers {
  $loadAndStart = Join-Path $DeployDir "load-and-start.sh"
  Write-LfFile -Path $loadAndStart -Lines @(
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
    'PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"',
    'IMAGE_DIR="$PACKAGE_DIR/images"',
    "",
    'if ! command -v docker >/dev/null 2>&1; then',
    '  echo "Docker is not installed or not in PATH." >&2',
    "  exit 1",
    "fi",
    "",
    'if [ ! -d "$IMAGE_DIR" ]; then',
    '  echo "Missing image directory: $IMAGE_DIR" >&2',
    "  exit 1",
    "fi",
    "",
    'cd "$SCRIPT_DIR"',
    'mkdir -p mysql_data minio_data esdata01 ragflow-logs redis_data',
    'chmod 777 mysql_data minio_data esdata01 ragflow-logs redis_data || true',
    'for image_tar in "$IMAGE_DIR"/*.tar; do',
    '  [ -e "$image_tar" ] || { echo "No image tar files found in $IMAGE_DIR" >&2; exit 1; }',
    '  case "$image_tar" in *.rootfs.tar) continue ;; esac',
    '  echo "Loading Docker image: $image_tar"',
    '  docker load -i "$image_tar"',
    "done",
    "",
    'RAGFLOW_ROOTFS="$IMAGE_DIR/infiniflow_ragflow_v0.25.6.rootfs.tar"',
    'if [ -f "$RAGFLOW_ROOTFS" ]; then',
    '  echo "Importing RAGFlow official rootfs as infiniflow/ragflow:v0.25.6 ..."',
    '  docker import \',
    '    --change "USER root" \',
    '    --change "WORKDIR /ragflow" \',
    '    --change "ENTRYPOINT [\"./entrypoint.sh\"]" \',
    '    --change "ENV PATH=/ragflow/.venv/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \',
    '    --change "ENV VIRTUAL_ENV=/ragflow/.venv" \',
    '    --change "ENV PYTHONPATH=/ragflow/" \',
    '    --change "ENV PYTHONDONTWRITEBYTECODE=1" \',
    '    --change "ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1" \',
    '    "$RAGFLOW_ROOTFS" infiniflow/ragflow:v0.25.6',
    "else",
    '  echo "Missing RAGFlow rootfs tar: $RAGFLOW_ROOTFS" >&2',
    "  exit 1",
    "fi",
    'echo "Starting RAGFlow v0.25.6 official CPU + Elasticsearch stack ..."',
    "docker compose up -d",
    "docker compose ps",
    'echo "Done. Open: http://<Ubuntu-IP>:8013"'
  )

  $start = Join-Path $DeployDir "start.sh"
  Write-LfFile -Path $start -Lines @(
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    'cd "$(dirname "${BASH_SOURCE[0]}")"',
    'mkdir -p mysql_data minio_data esdata01 ragflow-logs redis_data',
    'chmod 777 mysql_data minio_data esdata01 ragflow-logs redis_data || true',
    "docker compose up -d",
    "docker compose ps"
  )

  $stop = Join-Path $DeployDir "stop.sh"
  Write-LfFile -Path $stop -Lines @(
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    'cd "$(dirname "${BASH_SOURCE[0]}")"',
    "docker compose stop"
  )

  $health = Join-Path $DeployDir "healthcheck.sh"
  Write-LfFile -Path $health -Lines @(
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    'cd "$(dirname "${BASH_SOURCE[0]}")"',
    "docker compose ps",
    "curl -fsS http://127.0.0.1:8013/api/v1/system/version || true",
    "echo",
    "curl -fsS http://127.0.0.1:8013/api/v1/system/healthz || true",
    "echo"
  )

  $readme = Join-Path $PackageRoot "README_OFFLINE_UBUNTU.md"
  Write-LfFile -Path $readme -Lines @(
    "# RAGFlow v0.25.6 Official Offline Package",
    "",
    "This is a clean official deployment package. It does not include old RAGFlow data.",
    "",
    "## Contents",
    "",
    "- images/*.tar: offline Docker images for base services",
    "- images/infiniflow_ragflow_v0.25.6.rootfs.tar: RAGFlow official container rootfs, imported as infiniflow/ragflow:v0.25.6",
    "- ragflow-v0.25.6-offline/: Docker Compose deployment directory",
    "- IMAGE-MANIFEST.txt: image list and checksum",
    "",
    "## Start On Ubuntu",
    "",
    "cd ragflow-v0.25.6-offline-package-8013-clean/ragflow-v0.25.6-offline",
    "bash load-and-start.sh",
    "",
    "Open in browser:",
    "",
    "http://<Ubuntu-IP>:8013",
    "",
    "Default login:",
    "",
    "Username: admin@ragflow.io",
    "Password: admin",
    "",
    "## Common Commands",
    "",
    "cd ragflow-v0.25.6-offline",
    "bash start.sh",
    "bash stop.sh",
    "bash healthcheck.sh",
    "docker compose logs -f ragflow-cpu",
    "",
    "## Ports",
    "",
    "- Web: 8013",
    "- API: 29380",
    "- MySQL: 5457",
    "- MinIO: 9012, 9013",
    "- Redis: 6382",
    "- Elasticsearch: 12102",
    "",
    "If a port conflicts on the customer machine, edit the matching value in ragflow-v0.25.6-offline/.env, then run bash start.sh.",
    "",
    "## Notes",
    "",
    "- This package includes only the official RAGFlow service and base dependency images.",
    "- Ollama and local LLM model files are not included.",
    "- If the customer site also needs local models, prepare Ollama and model files separately.",
    "- You may change default passwords in .env before the first startup.",
    "- Do not casually change MySQL/MinIO/Redis/ES passwords after data already exists."
  )
}

function Save-Images {
  New-Item -ItemType Directory -Force -Path $ImageDir | Out-Null

  $oldCombinedTar = Join-Path $PackageRoot "ragflow-v0.25.6-images.tar"
  if (Test-Path -LiteralPath $oldCombinedTar) {
    Remove-Item -LiteralPath $oldCombinedTar -Force
  }

  foreach ($image in $Images) {
    Run-Native -WorkingDirectory $SourceDir -Exe "docker" -CommandArgs @("image", "inspect", "--format", "{{.Id}} {{.Size}}", $image) -TimeoutSeconds 120
  }

  $manifestLines = [System.Collections.Generic.List[string]]::new()
  $manifestLines.Add("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
  $manifestLines.Add("Package mode: base images as docker save tar; RAGFlow as exported official rootfs")
  $manifestLines.Add("")
  $manifestLines.Add("Images:")

  foreach ($image in $Images) {
    $safeName = $image -replace '[^A-Za-z0-9._-]', '_'
    $tarPath = Join-Path $ImageDir ($safeName + ".tar")
    if ((Test-Path -LiteralPath $tarPath) -and ((Get-Item -LiteralPath $tarPath).Length -gt 0)) {
      Log "Reusing existing non-empty image tar: $tarPath"
    } elseif (Test-Path -LiteralPath $tarPath) {
      Remove-Item -LiteralPath $tarPath -Force
      Log "Saving Docker image to tar: $image -> $tarPath"
      Run-CmdRedirect -WorkingDirectory $SourceDir -CommandLine ("docker save {0} > ""{1}""" -f $image, $tarPath) -TimeoutSeconds 7200
    } else {
      Log "Saving Docker image to tar: $image -> $tarPath"
      Run-CmdRedirect -WorkingDirectory $SourceDir -CommandLine ("docker save {0} > ""{1}""" -f $image, $tarPath) -TimeoutSeconds 7200
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $tarPath).Hash
    $size = (Get-Item -LiteralPath $tarPath).Length
    $sizeGb = [Math]::Round($size / 1GB, 2)
    $manifestLines.Add("- $image")
    $manifestLines.Add("  file: images/$safeName.tar")
    $manifestLines.Add("  size_bytes: $size")
    $manifestLines.Add("  size_gb: $sizeGb")
    $manifestLines.Add("  sha256: $hash")
  }

  if ((Test-Path -LiteralPath $RagflowRootfsTar) -and ((Get-Item -LiteralPath $RagflowRootfsTar).Length -gt 0)) {
    Log "Reusing existing non-empty RAGFlow rootfs tar: $RagflowRootfsTar"
  } elseif (Test-Path -LiteralPath $RagflowRootfsTar) {
    Remove-Item -LiteralPath $RagflowRootfsTar -Force
    Log "Exporting RAGFlow official container rootfs: $RagflowContainer -> $RagflowRootfsTar"
    Run-CmdRedirect -WorkingDirectory $SourceDir -CommandLine ("docker export {0} > ""{1}""" -f $RagflowContainer, $RagflowRootfsTar) -TimeoutSeconds 7200
  } else {
    Log "Exporting RAGFlow official container rootfs: $RagflowContainer -> $RagflowRootfsTar"
    Run-CmdRedirect -WorkingDirectory $SourceDir -CommandLine ("docker export {0} > ""{1}""" -f $RagflowContainer, $RagflowRootfsTar) -TimeoutSeconds 7200
  }

  $rootfsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $RagflowRootfsTar).Hash
  $rootfsSize = (Get-Item -LiteralPath $RagflowRootfsTar).Length
  $rootfsSizeGb = [Math]::Round($rootfsSize / 1GB, 2)
  $manifestLines.Add("- infiniflow/ragflow:v0.25.6")
  $manifestLines.Add("  file: images/infiniflow_ragflow_v0.25.6.rootfs.tar")
  $manifestLines.Add("  mode: docker export rootfs from official running container, import on target")
  $manifestLines.Add("  size_bytes: $rootfsSize")
  $manifestLines.Add("  size_gb: $rootfsSizeGb")
  $manifestLines.Add("  sha256: $rootfsHash")

  $manifestPath = Join-Path $PackageRoot "IMAGE-MANIFEST.txt"
  Write-LfFile -Path $manifestPath -Lines $manifestLines.ToArray()
}

"=== package-ragflow-v0256-offline-8013 started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

try {
  Copy-DeploymentSkeleton
  Normalize-Env
  Write-OfflineHelpers

  Log "Validating packaged compose config."
  Run-Native -WorkingDirectory $DeployDir -Exe "docker" -CommandArgs @("compose", "config") -TimeoutSeconds 120

  Save-Images

  Log "Offline package ready: $PackageRoot"
  Log "Copy this whole directory to the Ubuntu machine: $PackageRoot"
  exit 0
} catch {
  Log ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
