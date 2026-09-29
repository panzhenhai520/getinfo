$ErrorActionPreference = "Continue"

$LogPath = "D:\docker-data\ragflow\cleanup-ragflow-offline-package-saves.log"
"=== cleanup ragflow offline package saves $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

Get-CimInstance Win32_Process |
  Where-Object {
    (
      ($_.Name -like "docker*.exe" -and $_.CommandLine -like "* save *") -or
      ($_.Name -in @("cmd.exe", "powershell.exe") -and $_.CommandLine -like "*ragflow-v0.25.6-offline-package-8013*") -or
      ($_.CommandLine -like "*infiniflow/ragflow:v0.25.6*")
    )
  } |
  ForEach-Object {
    "Terminating PID=$($_.ProcessId) Name=$($_.Name) CommandLine=$($_.CommandLine)" |
      Add-Content -LiteralPath $LogPath -Encoding UTF8
    try {
      Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-String |
        Add-Content -LiteralPath $LogPath -Encoding UTF8
    } catch {
      "Failed: $($_.Exception.Message)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
    }
  }

"Done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Add-Content -LiteralPath $LogPath -Encoding UTF8
exit 0
