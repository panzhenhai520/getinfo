# Remove the CollectInfo scheduled task (run as Administrator).
$taskName = 'CollectInfoService'
try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed scheduled task [$taskName]." -ForegroundColor Green
} catch {
    Write-Host "Task [$taskName] not found or removal failed: $($_.Exception.Message)" -ForegroundColor Yellow
}
