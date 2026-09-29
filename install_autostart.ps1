# Register CollectInfo service as a Windows Scheduled Task (auto-start on boot + auto-restart on crash).
# Run this as Administrator.
$ErrorActionPreference = 'Stop'
$taskName = 'CollectInfoService'
$python = 'C:\Anaconda\python.exe'
$scriptPath = 'F:\CollectInfo\start_with_schedule.py'
$workDir = 'F:\CollectInfo'

if (-not (Test-Path $python)) { throw "python not found: $python" }
if (-not (Test-Path $scriptPath)) { throw "startup script not found: $scriptPath" }

Write-Host "Registering scheduled task [$taskName] ..."
try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

$action  = New-ScheduledTaskAction -Execute $python -Argument ("`"$scriptPath`"") -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Description 'CollectInfo main service (auto-start + auto-restart)' | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Registered and started [$taskName]. It will auto-run collectinfo at boot and auto-restart (up to 3x, 1min apart) after crash." -ForegroundColor Green
