$ErrorActionPreference = "Stop"

$NewDir = "D:\docker-data\ragflow\ragflow-v0.25.6-upgrade"
$LogPath = Join-Path $NewDir "resume-side-by-side-246.log"
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

function Ensure-CustomImage {
  $selectedBase = $null
  foreach ($image in $BaseImages) {
    Log "Checking base image: $image"
    try {
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", $image) -TimeoutSeconds 60
      $selectedBase = $image
      break
    } catch {
      Log "Base image not found locally: $image"
    }

    Log "Pulling base image: $image"
    try {
      Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("pull", $image) -TimeoutSeconds 1800
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
  ) -TimeoutSeconds 1800
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("image", "inspect", "ragflow:v0.25.6-custom") -TimeoutSeconds 60
}

"=== resume-side-by-side-246 started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

try {
  if (-not (Test-Path -LiteralPath $NewDir -PathType Container)) {
    throw "Missing NewDir: $NewDir"
  }
  if (-not (Test-Path -LiteralPath (Join-Path $NewDir "backend-image") -PathType Container)) {
    throw "Missing backend-image directory."
  }
  if (-not (Test-Path -LiteralPath (Join-Path $NewDir "app\web\dist") -PathType Container)) {
    throw "Missing custom frontend dist directory."
  }

  Ensure-CustomImage

  Log "Validating v0.25.6 compose config."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "config") -TimeoutSeconds 120

  Log "Starting v0.25.6 side by side."
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "up", "-d") -TimeoutSeconds 1800

  Log "New v0.25.6 status:"
  Run-Native -WorkingDirectory $NewDir -Exe "docker" -CommandArgs @("compose", "ps") -TimeoutSeconds 120

  Log "Resume deployment finished. New web should be on :8003."
  exit 0
} catch {
  Log ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
