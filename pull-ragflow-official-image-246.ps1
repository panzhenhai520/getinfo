$ErrorActionPreference = "Continue"

$LogPath = "D:\docker-data\ragflow\pull-ragflow-official-image.log"
"=== pull official RAGFlow image $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

Push-Location "D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013"
try {
  docker pull infiniflow/ragflow:v0.25.6 2>&1 |
    ForEach-Object { $_.ToString() } |
    Tee-Object -FilePath $LogPath -Append
  $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  "docker pull exit: $code" | Add-Content -LiteralPath $LogPath -Encoding UTF8

  docker image inspect --format "{{.Id}} {{.Size}}" infiniflow/ragflow:v0.25.6 2>&1 |
    ForEach-Object { $_.ToString() } |
    Tee-Object -FilePath $LogPath -Append
} finally {
  Pop-Location
}

exit $code
