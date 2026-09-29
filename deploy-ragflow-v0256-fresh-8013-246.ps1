$ErrorActionPreference = "Stop"

$SourceDir = "D:\docker-data\ragflow\ragflow-v0.25.6-upgrade"
$NewDir = "D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013"
$ProjectName = "ragflow-v0256-fresh-8013"
$CustomImage = "ragflow:v0.25.6-custom-fresh-8013"
$ParentDir = Split-Path -Parent $NewDir
$LogPath = Join-Path $ParentDir "deploy-ragflow-v0256-fresh-8013.log"
$MarkerPath = Join-Path $NewDir ".codex-fresh-8013.marker"

$PortPlan = [ordered]@{
  ES_PORT = "12102"
  OS_PORT = "12103"
  KIBANA_PORT = "6611"
  INFINITY_THRIFT_PORT = "23827"
  INFINITY_HTTP_PORT = "23830"
  INFINITY_PSQL_PORT = "55432"
  OCEANBASE_PORT = "12881"
  SEEKDB_PORT = "12882"
  EXPOSE_MYSQL_PORT = "5457"
  MINIO_PORT = "9012"
  MINIO_CONSOLE_PORT = "9013"
  REDIS_PORT = "6382"
  SVR_WEB_HTTP_PORT = "8013"
  SVR_WEB_HTTPS_PORT = "28443"
  SVR_HTTP_PORT = "29380"
  ADMIN_SVR_HTTP_PORT = "29381"
  SVR_MCP_PORT = "29382"
  GO_HTTP_PORT = "29384"
  GO_ADMIN_PORT = "29383"
  TEI_PORT = "6383"
  CUSTOM_SERVER_PORT = "3021"
  SANDBOX_EXECUTOR_MANAGER_PORT = "29385"
}

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

function Assert-Directory {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "Missing directory: $Path"
  }
}

function Assert-InsideNewDir {
  param([string]$Path)
  $newFull = [System.IO.Path]::GetFullPath($NewDir).TrimEnd('\') + '\'
  $targetFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
  if (-not $targetFull.StartsWith($newFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write outside NewDir: $Path"
  }
}

function Copy-TemplateWithoutRuntimeData {
  Assert-Directory $SourceDir

  if (-not (Test-Path -LiteralPath $NewDir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $NewDir | Out-Null
  }

  if ((Test-Path -LiteralPath (Join-Path $NewDir "docker-compose.yml")) -and -not (Test-Path -LiteralPath $MarkerPath)) {
    throw "Target already contains compose files but is not marked as this fresh deployment: $NewDir"
  }

  if (-not (Test-Path -LiteralPath (Join-Path $NewDir "docker-compose.yml"))) {
    Log "Copying template package, excluding runtime data directories."
    & robocopy $SourceDir $NewDir /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:16 /NP `
      /XD mysql_data minio_data esdata01 osdata01 infinity_data oceanbase ragflow-logs redis_data seekdb kibana_data `
      /XF *.log *.runner.log remote-echo-test.txt 2>&1 |
      Tee-Object -FilePath $LogPath -Append
    $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Log ("ROBOCOPY EXIT {0}" -f $code)
    if ($code -ge 8) {
      throw "Robocopy failed with exit code ${code}"
    }
  } else {
    Log "Template already copied; reusing marked fresh directory."
  }

  Set-Content -LiteralPath $MarkerPath -Value "Created by deploy-ragflow-v0256-fresh-8013-246.ps1" -Encoding UTF8

  foreach ($dir in @("mysql_data", "minio_data", "esdata01", "ragflow-logs", "redis_data", "custom_server\logs")) {
    $target = Join-Path $NewDir $dir
    Assert-InsideNewDir $target
    New-Item -ItemType Directory -Force -Path $target | Out-Null
  }

  $sourceRedisConf = Join-Path $SourceDir "redis_data\redis.conf"
  $targetRedisConf = Join-Path $NewDir "redis_data\redis.conf"
  Assert-InsideNewDir $targetRedisConf
  if (Test-Path -LiteralPath $sourceRedisConf) {
    Copy-Item -LiteralPath $sourceRedisConf -Destination $targetRedisConf -Force
  } elseif (-not (Test-Path -LiteralPath $targetRedisConf)) {
    Set-Content -LiteralPath $targetRedisConf -Value @(
      "protected-mode yes",
      "port 6379",
      "dir ./",
      "requirepass infini_rag_flow"
    ) -Encoding ASCII
  }
}

function Set-EnvValue {
  param(
    [string]$EnvPath,
    [string]$Key,
    [string]$Value
  )

  $lines = [System.Collections.Generic.List[string]]::new()
  if (Test-Path -LiteralPath $EnvPath) {
    foreach ($line in (Get-Content -LiteralPath $EnvPath)) {
      $lines.Add($line)
    }
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

function Configure-Env {
  $envPath = Join-Path $NewDir ".env"
  if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing .env after template copy: $envPath"
  }

  Set-EnvValue -EnvPath $envPath -Key "COMPOSE_PROJECT_NAME" -Value $ProjectName
  Set-EnvValue -EnvPath $envPath -Key "DOC_ENGINE" -Value "elasticsearch"
  Set-EnvValue -EnvPath $envPath -Key "DEVICE" -Value "cpu"
  Set-EnvValue -EnvPath $envPath -Key "COMPOSE_PROFILES" -Value "elasticsearch,cpu"
  Set-EnvValue -EnvPath $envPath -Key "RAGFLOW_IMAGE" -Value $CustomImage

  foreach ($item in $PortPlan.GetEnumerator()) {
    Set-EnvValue -EnvPath $envPath -Key $item.Key -Value $item.Value
  }

  $readmePath = Join-Path $NewDir "FRESH-8013-NOTES.txt"
  Assert-InsideNewDir $readmePath
  Set-Content -LiteralPath $readmePath -Encoding UTF8 -Value @(
    "Fresh RAGFlow v0.25.6 deployment for 246.",
    "This directory is independent from ragflow and ragflow-v0.25.6-upgrade.",
    "Project: $ProjectName",
    "Image: $CustomImage",
    "Web: http://192.168.1.246:8013",
    "API: http://192.168.1.246:29380",
    "MySQL: 5457",
    "MinIO: 9012/9013",
    "Redis: 6382",
    "Custom server: 3021"
  )
}

function Check-PortPlan {
  Log "Current docker containers before starting fresh deployment:"
  Run-Native -WorkingDirectory $ParentDir -Exe "docker" -CommandArgs @("ps", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}") -TimeoutSeconds 120

  Log "Checking desired host ports with netstat."
  $wantedPorts = @($PortPlan.Values | Select-Object -Unique)
  $netstatOutput = (& netstat -ano) -join "`n"
  foreach ($port in $wantedPorts) {
    if ($netstatOutput -match "[:\.]$([regex]::Escape($port))\s") {
      throw "Desired host port appears to be in use: $port"
    }
  }
}

function Ensure-CustomImage {
  try {
    Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $CustomImage) -TimeoutSeconds 60
    Log "Custom image already exists: $CustomImage"
    return
  } catch {
    Log "Custom image is not present yet: $CustomImage"
  }

  $baseImages = @(
    "ragflow:v0.25.6-custom",
    "infiniflow/ragflow:v0.25.6",
    "swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/infiniflow/ragflow:v0.25.6",
    "registry.cn-hangzhou.aliyuncs.com/infiniflow/ragflow:v0.25.6",
    "swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow:v0.25.6"
  )

  $selectedBase = $null
  foreach ($image in $baseImages) {
    Log "Checking base image: $image"
    try {
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $image) -TimeoutSeconds 60
      $selectedBase = $image
      break
    } catch {
      Log "Base image not found locally: $image"
    }

    if ($image -ne "ragflow:v0.25.6-custom") {
      Log "Pulling base image: $image"
      try {
        Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("pull", $image) -TimeoutSeconds 1800
        $selectedBase = $image
        break
      } catch {
        Log "Failed to pull base image: $image"
      }
    }
  }

  if (-not $selectedBase) {
    throw "Could not find or pull any v0.25.6 base image."
  }

  Log "Building dedicated custom image $CustomImage from $selectedBase"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @(
    "build",
    "--build-arg", "BASE_IMAGE=$selectedBase",
    "-t", $CustomImage,
    ".\backend-image"
  ) -TimeoutSeconds 1800
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $CustomImage) -TimeoutSeconds 60
}

"=== deploy-ragflow-v0256-fresh-8013 started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

try {
  Assert-Directory $SourceDir
  Copy-TemplateWithoutRuntimeData
  Configure-Env
  Check-PortPlan
  Ensure-CustomImage

  Log "Validating fresh v0.25.6 compose config."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "config") -TimeoutSeconds 120

  Log "Starting fresh v0.25.6 deployment."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "up", "-d") -TimeoutSeconds 1800

  Log "Fresh v0.25.6 compose status:"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "ps") -TimeoutSeconds 120

  Log "Deployment finished. Fresh web should be on http://192.168.1.246:8013"
  exit 0
} catch {
  Log ("ERROR: {0}" -f $_.Exception.Message)
  Log "No old deployment directories were stopped, removed, or overwritten by this script."
  exit 1
}
