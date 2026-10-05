# Creates (or replaces) a Windows scheduled task that runs scripts\collect-once.ps1
# every 15 minutes for the current user. See docs/collector.md.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register-collector-task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register-collector-task.ps1 -Minutes 30
#
# Remove it again with:
#   Unregister-ScheduledTask -TaskName "SITT live collector" -Confirm:$false
param(
    [int]$Minutes = 15,
    [string]$TaskName = "SITT live collector"
)
$ErrorActionPreference = "Stop"

if ($Minutes -lt 15) {
    Write-Error "Don't poll more often than every 15 minutes (see docs/data-sources.md)."
    exit 2
}

$script = Join-Path $PSScriptRoot "collect-once.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
# Catch up after sleep, never run two at once, and give up on a run after 5 minutes.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Should I Take This Train: collect live running status every $Minutes minutes." `
    -Force | Out-Null

Write-Output "Registered '$TaskName' to run every $Minutes minutes while you are signed in."
Write-Output "Log: data\logs\collector.log"
