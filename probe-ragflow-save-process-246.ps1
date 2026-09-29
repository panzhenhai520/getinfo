$ErrorActionPreference = "Continue"

$LogPath = "D:\docker-data\ragflow\probe-ragflow-save-process.log"
"=== probe ragflow save process $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
  Set-Content -LiteralPath $LogPath -Encoding UTF8

$procs = Get-CimInstance Win32_Process |
  Where-Object {
    ($_.Name -like "docker*.exe" -and ($_.CommandLine -like "* save *" -or $_.CommandLine -like "* export *")) -or
    ($_.CommandLine -like "*infiniflow/ragflow:v0.25.6*")
  }

foreach ($p in $procs) {
  "PID=$($p.ProcessId) Name=$($p.Name) Parent=$($p.ParentProcessId) CommandLine=$($p.CommandLine)" |
    Add-Content -LiteralPath $LogPath -Encoding UTF8
  try {
    Get-Process -Id $p.ProcessId |
      Select-Object Id,ProcessName,StartTime,CPU,PM,WS,Path |
      Format-List |
      Out-String |
      Add-Content -LiteralPath $LogPath -Encoding UTF8
  } catch {
    "Get-Process failed: $($_.Exception.Message)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }
}

if (-not $procs) {
  "No matching docker save process found." | Add-Content -LiteralPath $LogPath -Encoding UTF8
}

"Done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Add-Content -LiteralPath $LogPath -Encoding UTF8
exit 0
