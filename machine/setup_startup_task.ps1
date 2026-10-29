# Run this script as Administrator to register the startup task
# Right-click > Run with PowerShell (as Administrator)

$TaskName   = "SFA_Attendance_Server"
$VbsPath    = "C:\machine\start_server.vbs"
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

# Run as the current user even when nobody is logged in (after a power cut / reboot).
# Same Windows user is kept so SQL Server Windows authentication keeps working.
# Windows needs this user's password once to store it with the task.
$UserId = "$env:USERDOMAIN\$env:USERNAME"
$Cred   = Get-Credential -UserName $UserId -Message "Enter the Windows password for $UserId"
if (-not $Cred) {
    Write-Host "Cancelled - task not registered." -ForegroundColor Red
    pause
    exit 1
}

try {
    Register-ScheduledTask `
        -TaskName   $TaskName `
        -Action     $Action `
        -Trigger    $Trigger `
        -Settings   $Settings `
        -User       $UserId `
        -Password   $Cred.GetNetworkCredential().Password `
        -RunLevel   Highest `
        -Description $Description `
        -Force `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Host ""
    Write-Host "Task registration failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Check the password and that this window is running as Administrator." -ForegroundColor Yellow
    pause
    exit 1
}

Write-Host ""
Write-Host "Task registered successfully!" -ForegroundColor Green
Write-Host "Task Name : $TaskName" -ForegroundColor Cyan
Write-Host "Runs      : At every Windows startup (10s delay), no login needed" -ForegroundColor Cyan
Write-Host "User      : $UserId" -ForegroundColor Cyan
Write-Host "Script    : $VbsPath" -ForegroundColor Cyan
Write-Host ""
Write-Host "To remove this task later, run:" -ForegroundColor Yellow
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor Yellow
Write-Host ""
pause
