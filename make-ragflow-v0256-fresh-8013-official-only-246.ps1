$ErrorActionPreference = "Stop"

$NewDir = "D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013"
$ProjectName = "ragflow-v0256-fresh-8013"
$OfficialImage = "infiniflow/ragflow:v0.25.6"
$FreshCustomImage = "ragflow:v0.25.6-custom-fresh-8013"
$CustomContainer = "$ProjectName-custom_server-1"
$LogPath = "D:\docker-data\ragflow\make-ragflow-v0256-fresh-8013-official-only.log"
$OfficialMirrorImages = @(
  "swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/infiniflow/ragflow:v0.25.6",
  "registry.cn-hangzhou.aliyuncs.com/infiniflow/ragflow:v0.25.6",
  "swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow:v0.25.6"
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
    [bool]$AllowFailure = $false,
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
  if ($code -ne 0 -and -not $AllowFailure) {
    throw "Command failed with exit code ${code}: $Exe $($CommandArgs -join ' ')"
  }
}

function Assert-InsideNewDir {
  param([string]$Path)
  $newFull = [System.IO.Path]::GetFullPath($NewDir).TrimEnd('\') + '\'
  $targetFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
  if (-not $targetFull.StartsWith($newFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to modify outside NewDir: $Path"
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

function Remove-CustomServerService {
  $composePath = Join-Path $NewDir "docker-compose.yml"
  Assert-InsideNewDir $composePath
  $backupPath = Join-Path $NewDir ("docker-compose.yml.before-official-only-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
  Copy-Item -LiteralPath $composePath -Destination $backupPath -Force

  $out = [System.Collections.Generic.List[string]]::new()
  $skip = $false
  foreach ($line in (Get-Content -LiteralPath $composePath)) {
    if (-not $skip -and $line -match '^\s{2}custom_server:\s*$') {
      $skip = $true
      Log "Removing custom_server service block from docker-compose.yml"
      continue
    }

    if ($skip) {
      if ($line -match '^\s{2}# executor:' -or $line -match '^\S') {
        $skip = $false
        $out.Add($line)
      }
      continue
    }

    if ($line -match '^\s+-\s+\./app/web/dist:/ragflow/web/dist:ro\s*$') {
      Log "Removing custom frontend dist mount from docker-compose.yml"
      continue
    }

    $out.Add($line)
  }

  Set-Content -LiteralPath $composePath -Value $out -Encoding UTF8
}

function Remove-CustomNginxRoute {
  $nginxPath = Join-Path $NewDir "nginx\ragflow.conf"
  Assert-InsideNewDir $nginxPath
  if (-not (Test-Path -LiteralPath $nginxPath)) {
    return
  }

  $backupPath = Join-Path $NewDir ("nginx\ragflow.conf.before-official-only-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
  Copy-Item -LiteralPath $nginxPath -Destination $backupPath -Force

  $out = [System.Collections.Generic.List[string]]::new()
  $skip = $false
  foreach ($line in (Get-Content -LiteralPath $nginxPath)) {
    if (-not $skip -and $line -match '^\s*location\s+~\s+\^/\(custom\)\s+\{\s*$') {
      $skip = $true
      Log "Removing /custom nginx proxy route."
      continue
    }

    if ($skip) {
      if ($line -match '^\s*}\s*$') {
        $skip = $false
      }
      continue
    }

    $out.Add($line)
  }

  Set-Content -LiteralPath $nginxPath -Value $out -Encoding UTF8
}

function Ensure-OfficialImage {
  Log "Ensuring official RAGFlow image is present: $OfficialImage"
  try {
    Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $OfficialImage) -TimeoutSeconds 60
    return
  } catch {
    Log "Official Docker Hub tag is not present locally."
  }

  try {
    Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("pull", $OfficialImage) -TimeoutSeconds 1800
    return
  } catch {
    Log "Docker Hub pull failed; trying mirror tags for the same RAGFlow v0.25.6 image."
  }

  foreach ($mirrorImage in $OfficialMirrorImages) {
    try {
      Log "Trying mirror image: $mirrorImage"
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("pull", $mirrorImage) -TimeoutSeconds 1800
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("tag", $mirrorImage, $OfficialImage) -TimeoutSeconds 120
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $OfficialImage) -TimeoutSeconds 60
      return
    } catch {
      Log "Mirror failed: $mirrorImage"
    }
  }

  throw "Could not pull official RAGFlow v0.25.6 image from Docker Hub or configured mirrors."
}

"=== make-ragflow-v0256-fresh-8013-official-only started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

try {
  if (-not (Test-Path -LiteralPath $NewDir -PathType Container)) {
    throw "Missing fresh deployment directory: $NewDir"
  }

  Ensure-OfficialImage

  Set-EnvValue -EnvPath (Join-Path $NewDir ".env") -Key "RAGFLOW_IMAGE" -Value $OfficialImage
  Set-EnvValue -EnvPath (Join-Path $NewDir ".env") -Key "COMPOSE_PROFILES" -Value "elasticsearch,cpu"

  Remove-CustomServerService
  Remove-CustomNginxRoute

  Log "Removing custom_server container from the fresh project, if present."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("rm", "-f", $CustomContainer) -AllowFailure $true -TimeoutSeconds 120

  $customDir = Join-Path $NewDir "custom_server"
  Assert-InsideNewDir $customDir
  if (Test-Path -LiteralPath $customDir -PathType Container) {
    Log "Renaming custom_server directory so it is not part of this official-only deployment."
    $disabledDir = Join-Path $NewDir ("custom_server.disabled-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    try {
      Rename-Item -LiteralPath $customDir -NewName (Split-Path -Leaf $disabledDir)
    } catch {
      Log ("Could not rename custom_server directory; leaving it unused because compose no longer references it: {0}" -f $_.Exception.Message)
    }
  }

  Log "Validating official-only compose config."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "config") -TimeoutSeconds 120

  Log "Recreating fresh RAGFlow with official image and removing project orphans."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "up", "-d", "--remove-orphans") -TimeoutSeconds 1800

  Log "Removing dedicated custom image created for the fresh deployment, if no longer used."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "rm", $FreshCustomImage) -AllowFailure $true -TimeoutSeconds 120

  Log "Official-only fresh compose status:"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "ps") -TimeoutSeconds 120

  Log "Finished. Fresh RAGFlow official-only web: http://192.168.1.246:8013"
  exit 0
} catch {
  Log ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
