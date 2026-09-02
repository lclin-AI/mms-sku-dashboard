# Registers the two Windows scheduled tasks for the MMS SKU dashboard pipeline.
# Run in an ELEVATED PowerShell (Run as administrator), from this folder:
#   powershell -ExecutionPolicy Bypass -File register_tasks.ps1
#
# The MMS login uses the WindowsApps Python alias, which needs the user profile,
# so both tasks run as the current user, "run whether logged on or not" with the
# user's password prompted once by schtasks. If you prefer "only when logged on",
# swap -RunLevel/-LogonType accordingly.

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$daily  = Join-Path $here 'run_daily.bat'
$mon    = Join-Path $here 'run_monitor.bat'
$user   = "$env:USERDOMAIN\$env:USERNAME"

function Reg($name, $bat, $time) {
  $action  = New-ScheduledTaskAction  -Execute $bat -WorkingDirectory $here
  $trigger = New-ScheduledTaskTrigger -Daily -At $time
  $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
              -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $set -RunLevel Limited -User $user -Force
  Write-Host "registered: $name @ $time"
}

# Load at 07:00, verify freshness at 07:40. Adjust times as you like.
Reg 'MMS SKU Daily Load'    $daily '07:00'
Reg 'MMS SKU Load Monitor'  $mon   '07:40'

Write-Host ""
Write-Host "Done. Test now with:  Start-ScheduledTask -TaskName 'MMS SKU Daily Load'"
Write-Host "Then check run_daily.log in this folder."
