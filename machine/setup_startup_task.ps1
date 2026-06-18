# Run this script as Administrator to register the startup task
# Right-click > Run with PowerShell (as Administrator)

$TaskName   = "SFA_Attendance_Server"
$VbsPath    = "D:\attedenceBackup\machine\start_server.vbs"
$Description = "Auto-start SFA Attendance Django server on Windows startup"

# Remove old task if it exists
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action: run the VBS file silently
$Action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`""

# Trigger: at system startup, with a 10-second delay
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Trigger.Delay = "PT10S"   # 10-second delay so network is ready

# Settings
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

# Run as current logged-in user
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -Principal  $Principal `
    -Description $Description `
    -Force

Write-Host ""
Write-Host "Task registered successfully!" -ForegroundColor Green
Write-Host "Task Name : $TaskName" -ForegroundColor Cyan
Write-Host "Runs      : At every Windows startup (10s delay)" -ForegroundColor Cyan
Write-Host "Script    : $VbsPath" -ForegroundColor Cyan
Write-Host ""
Write-Host "To remove this task later, run:" -ForegroundColor Yellow
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor Yellow
Write-Host ""
pause
