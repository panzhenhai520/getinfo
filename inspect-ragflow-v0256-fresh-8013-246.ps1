$ErrorActionPreference = "Continue"

$NewDir = "D:\docker-data\ragflow\ragflow-v0.25.6-fresh-8013"
$LogPath = "D:\docker-data\ragflow\inspect-ragflow-v0256-fresh-8013.log"
$Container = "ragflow-v0256-fresh-8013-ragflow-cpu-1"

"=== inspect-ragflow-v0256-fresh-8013 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

Push-Location $NewDir
try {
  "=== docker compose ps ===" | Add-Content -LiteralPath $LogPath
  docker compose ps 2>&1 | Add-Content -LiteralPath $LogPath

  "=== docker ps fresh project ===" | Add-Content -LiteralPath $LogPath
  docker ps -a --filter "name=ragflow-v0256-fresh-8013" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1 |
    Add-Content -LiteralPath $LogPath

  "=== ragflow-cpu inspect state ===" | Add-Content -LiteralPath $LogPath
  docker inspect --format "{{json .State}}" $Container 2>&1 | Add-Content -LiteralPath $LogPath

  "=== ragflow-cpu logs ===" | Add-Content -LiteralPath $LogPath
  docker logs --tail=260 $Container 2>&1 | Add-Content -LiteralPath $LogPath

  "=== ragflow-cpu image ===" | Add-Content -LiteralPath $LogPath
  docker inspect --format "{{.Config.Image}}" $Container 2>&1 | Add-Content -LiteralPath $LogPath
} finally {
  Pop-Location
}

exit 0
