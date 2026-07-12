# Lightweight hourly snapshot: pull artifacts + append monitoring/timeline.jsonl
param(
    [int]$IntervalMinutes = 60,
    [string]$TaskName = "GrokBot1-PulseMonitor",
    [string]$RepoRoot = "C:\Users\tieut\Bot-1"
)

$ErrorActionPreference = "Stop"
$pull = Join-Path $RepoRoot "scripts\pulse-babysit\pull-vps-artifacts.ps1"
if (-not (Test-Path $pull)) {
    Write-Error "Missing $pull"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pull`" -SkipPush" `
    -WorkingDirectory $RepoRoot

$startAt = (Get-Date).AddMinutes(2)
$trigger = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName' every ${IntervalMinutes} min:"
Write-Host "  pull-vps-artifacts.ps1 -SkipPush  (timeline.jsonl + local latest)"
Write-Host "View timeline: python scripts\pulse-babysit\timeline-view.py"