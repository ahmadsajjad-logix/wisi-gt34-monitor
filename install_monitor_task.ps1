param(
    [string]$TaskName = "WISI GT34 Monitor",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Monitor = Join-Path $Root "monitor.py"

if (-not (Test-Path -LiteralPath $Monitor)) {
    throw "monitor.py not found: $Monitor"
}

# Resolve the real Python interpreter rather than the WindowsApps execution alias.
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = (& python -c "import sys; print(sys.executable)").Trim()
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    throw "Unable to resolve the Python interpreter path."
}

$Python = [System.IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}

# Refuse the WindowsApps alias because SYSTEM tasks cannot safely depend on it.
if ($Python -match '\\WindowsApps\\python(?:3)?\.exe$') {
    throw "Resolved Python is the WindowsApps execution alias, not the real interpreter: $Python"
}

# Validate that this interpreter can import the production monitor before task registration.
& $Python -c "import monitor; print('MONITOR IMPORT PASSED')"
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python interpreter cannot import monitor.py."
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

$Task = Get-ScheduledTask -TaskName $TaskName

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Python: $Python"
Write-Host "Project: $Root"
Write-Host "Run as: $($Task.Principal.UserId)"
Write-Host "Trigger: At startup"
Write-Host "State: $($Task.State)"
