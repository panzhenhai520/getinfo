$ErrorActionPreference = "Stop"

$OldDir = "D:\docker-data\ragflow\ragflow"
$NewDir = "D:\docker-data\ragflow\ragflow-v0.25.6-upgrade"
$LogPath = Join-Path $NewDir "deploy-side-by-side-246.log"
$BaseImages = @(
  "infiniflow/ragflow:v0.25.6",
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

function Assert-Directory {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "Missing directory: $Path"
  }
}

function Assert-TargetInsideNewDir {
  param([string]$Path)
  $newFull = [System.IO.Path]::GetFullPath($NewDir).TrimEnd('\') + '\'
  $targetFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
  if (-not $targetFull.StartsWith($newFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write outside NewDir: $Path"
  }
}

function Run-Native {
  param(
    [string]$WorkingDirectory,
    [string]$Exe,
    [string[]]$CommandArgs
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

function Run-Robocopy {
  param(
    [string]$Source,
    [string]$Destination,
    [bool]$AllowFailure = $false
  )

  Assert-Directory $Source
  Assert-TargetInsideNewDir $Destination
  New-Item -ItemType Directory -Force -Path $Destination | Out-Null

  Log "ROBOCOPY $Source -> $Destination"
  & robocopy $Source $Destination /MIR /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:16 /NP /XF *.sock /XD "#innodb_temp" 2>&1 |
    Tee-Object -FilePath $LogPath -Append
  $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  Log ("ROBOCOPY EXIT {0}" -f $code)

  if ($code -ge 8 -and -not $AllowFailure) {
    throw "Robocopy failed with exit code ${code}: $Source -> $Destination"
  }
}

function Copy-AllData {
  param([bool]$AllowFailure = $false)

  foreach ($name in @("mysql_data", "minio_data", "esdata01", "redis_data")) {
    Run-Robocopy -Source (Join-Path $OldDir $name) -Destination (Join-Path $NewDir $name) -AllowFailure $AllowFailure
  }

  $oldCustomConfig = Join-Path $OldDir "custom_server\config"
  $newCustomConfig = Join-Path $NewDir "custom_server\config"
  if (Test-Path -LiteralPath $oldCustomConfig -PathType Container) {
    Run-Robocopy -Source $oldCustomConfig -Destination $newCustomConfig -AllowFailure $AllowFailure
  } else {
    Log "Skip custom_server config copy because source does not exist: $oldCustomConfig"
  }
}

function Remove-MySqlRuntimeFilesFromNewCopy {
  $sock = Join-Path $NewDir "mysql_data\mysql.sock"
  if (Test-Path -LiteralPath $sock) {
    Assert-TargetInsideNewDir $sock
    Log "Removing runtime-only MySQL socket from new copy: $sock"
    Remove-Item -LiteralPath $sock -Force
  }

  $tempDir = Join-Path $NewDir "mysql_data\#innodb_temp"
  if (Test-Path -LiteralPath $tempDir) {
    Assert-TargetInsideNewDir $tempDir
    Log "Removing runtime-only MySQL temp directory from new copy: $tempDir"
    Remove-Item -LiteralPath $tempDir -Recurse -Force
  }
}

function Ensure-CustomImage {
  $selectedBase = $null
  foreach ($image in $BaseImages) {
    Log "Checking base image: $image"
    try {
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $image)
      $selectedBase = $image
      break
    } catch {
      Log "Base image not found locally: $image"
    }

    Log "Pulling base image: $image"
    try {
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("pull", $image)
      $selectedBase = $image
      break
    } catch {
      Log "Failed to pull base image: $image"
    }
  }

  if (-not $selectedBase) {
    throw "Could not find or pull any v0.25.6 base image."
  }

  Log "Building custom backend image from $selectedBase"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @(
    "build",
    "--build-arg", "BASE_IMAGE=$selectedBase",
    "-t", "ragflow:v0.25.6-custom",
    ".\backend-image"
  )
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", "ragflow:v0.25.6-custom")
}

New-Item -ItemType Directory -Force -Path $NewDir | Out-Null
"=== deploy-side-by-side-246 started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

$oldStopped = $false

try {
  Assert-Directory $OldDir
  Assert-Directory $NewDir
  Assert-Directory (Join-Path $NewDir "backend-image")
  Assert-Directory (Join-Path $NewDir "app\web\dist")

  Log "OldDir: $OldDir"
  Log "NewDir: $NewDir"
  Log "Pre-copying old v0.19 data while it is still running. Some locked-file errors are allowed in this pass."
  Copy-AllData -AllowFailure $true

  Log "Stopping old v0.19 for final consistent data sync."
  $oldStopped = $true
  Run-Native -WorkingDirectory $OldDir -Exe "docker" -CommandArgs @("compose", "down")

  Log "Final-syncing data into the independent v0.25.6 directory."
  Copy-AllData -AllowFailure $false
  Remove-MySqlRuntimeFilesFromNewCopy

  Log "Starting old v0.19 again before launching v0.25.6."
  Run-Native -WorkingDirectory $OldDir -Exe "docker" -CommandArgs @("compose", "up", "-d")
  $oldStopped = $false

  Ensure-CustomImage

  Log "Validating v0.25.6 compose config."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "config")

  Log "Starting v0.25.6 side by side."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "up", "-d")

  Log "Old v0.19 status:"
  Run-Native -WorkingDirectory $OldDir -Exe "docker" -CommandArgs @("compose", "ps")

  Log "New v0.25.6 status:"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "ps")

  Log "Deployment finished. Old web should remain on :8002. New web should be on :8003."
  exit 0
} catch {
  Log ("ERROR: {0}" -f $_.Exception.Message)
  if ($oldStopped) {
    Log "Old v0.19 was stopped when the error happened. Trying to start it again."
    try {
      Run-Native -WorkingDirectory $OldDir -Exe "docker" -CommandArgs @("compose", "up", "-d")
    } catch {
      Log ("FAILED TO RESTART OLD v0.19: {0}" -f $_.Exception.Message)
    }
  }
  exit 1
}
