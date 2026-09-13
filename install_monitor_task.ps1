param(
    [string]$TaskName = "WISI GT34 Monitor"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Monitor = Join-Path $Root "monitor.py"
$Python = (Get-Command python -ErrorAction Stop).Source

if (-not (Test-Path $Monitor)) {
    throw "monitor.py not found: $Monitor"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}" --interval 5' -f $Monitor) `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User "SYSTEM" `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Python: $Python"
Write-Host "Project: $Root"
