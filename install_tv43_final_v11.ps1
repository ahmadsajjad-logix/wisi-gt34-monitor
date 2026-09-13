$ErrorActionPreference = "Stop"

$Project = "C:\Users\ahmadsajjad\Desktop\Projects\wisi-gt34-monitor"
$Python = "C:\Users\ahmadsajjad\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Wrapper = "C:\Program Files (x86)\PRTG Network Monitor\Custom Sensors\EXEXML\wisi_gt34_carrier.bat"
$TaskName = "WISI GT34 TV43 Alarm Policy"

$SensorSource = Join-Path $PSScriptRoot "prtg_tv43_sensor.py"
$HistorySource = Join-Path $PSScriptRoot "tv43_history.py"
$HistoryValidatorSource = Join-Path $PSScriptRoot "validate_tv43_history.py"
$AlarmSource = Join-Path $PSScriptRoot "tv43_alarm_policy_final.py"

$SensorTarget = Join-Path $Project "prtg_tv43_sensor.py"
$HistoryTarget = Join-Path $Project "tv43_history.py"
$HistoryValidatorTarget = Join-Path $Project "validate_tv43_history.py"
$AlarmTarget = Join-Path $Project "tv43_alarm_policy_final.py"
$Manifest = Join-Path $Project "prtg_tv43_deployment\tv43_created_sensors.csv"
$StateDir = Join-Path $Project "prtg_tv43_deployment"

Write-Host ""
Write-Host "WISI GT34 TV43 FINAL MONITORING + EMAIL POLICY V11" -ForegroundColor Cyan
Write-Host "Rich bounded PRTG + 15-day SQLite history + final immediate alert/recovery policy" -ForegroundColor Cyan
Write-Host ""

foreach ($f in @(
    $SensorSource,$HistorySource,$HistoryValidatorSource,$AlarmSource,
    $Wrapper,$Manifest,
    (Join-Path $Project "email_notifier.py"),
    (Join-Path $Project "database\wisi_monitor.db"),
    (Join-Path $Project "authoritative_inventory_v6\active_services.csv")
)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Required file missing: $f" }
    Write-Host "FOUND: $f"
}

$rows = @(Import-Csv -LiteralPath $Manifest)
if ($rows.Count -ne 43) { throw "Expected 43 TV43 sensors; found $($rows.Count)" }

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"

foreach ($pair in @(
    @($SensorTarget,"prtg_tv43_sensor.py"),
    @($HistoryTarget,"tv43_history.py"),
    @($AlarmTarget,"tv43_alarm_policy_final.py")
)) {
    if (Test-Path -LiteralPath $pair[0]) {
        $backup = Join-Path $StateDir ($pair[1] + ".before_final_v11_" + $stamp)
        Copy-Item -LiteralPath $pair[0] -Destination $backup -Force
        Write-Host "Backup: $backup"
    }
}

Copy-Item $SensorSource $SensorTarget -Force
Copy-Item $HistorySource $HistoryTarget -Force
Copy-Item $HistoryValidatorSource $HistoryValidatorTarget -Force
Copy-Item $AlarmSource $AlarmTarget -Force

& $Python -m py_compile $SensorTarget $HistoryTarget $HistoryValidatorTarget $AlarmTarget
if ($LASTEXITCODE -ne 0) { throw "Final V11 Python compilation failed." }

Write-Host ""
Write-Host "Refreshing and validating all 43 authoritative carriers through the ACTUAL PRTG wrapper..."
$wrapperFailures = @()
$realFaults = @()
$maxChannels = 0

foreach ($row in $rows) {
    $raw = & $Wrapper --tv43 --host ([string]$row.host) --module ([string]$row.module) --channel ([string]$row.channel) 2>&1
    if ($LASTEXITCODE -ne 0) {
        $wrapperFailures += "$($row.sensor_name): wrapper exit $LASTEXITCODE"
        continue
    }
    try { $obj = (($raw -join "`n").Trim() | ConvertFrom-Json) }
    catch {
        $wrapperFailures += "$($row.sensor_name): invalid JSON"
        continue
    }
    if ($obj.prtg.error) {
        $wrapperFailures += "$($row.sensor_name): $($obj.prtg.text)"
        continue
    }

    $channels = @($obj.prtg.result)
    if ($channels.Count -gt $maxChannels) { $maxChannels = $channels.Count }
    if ($channels.Count -gt 50) {
        $wrapperFailures += "$($row.sensor_name): $($channels.Count) channels > 50"
        continue
    }

    $map = @{}
    foreach ($c in $channels) { $map[[string]$c.channel] = $c.value }

    foreach ($required in @(
        "Carrier / TS Health","Demod Lock","Transport Stream Present",
        "Service Integrity Health","RF Level","SNR","BER","Frequency",
        "Symbol Rate","TSID","NID","ONID","Discovered Services",
        "Running Services","Missing Services","Unexpected Services",
        "Service Name Mismatches","ES Metadata Unavailable"
    )) {
        if (-not $map.ContainsKey($required)) {
            $wrapperFailures += "$($row.sensor_name): missing '$required'"
        }
    }

    if ([int]$map["Service Integrity Health"] -eq 0) {
        $realFaults += [string]$row.sensor_name
        Write-Host ("REAL MONITORING FAULT: {0}" -f $row.sensor_name) -ForegroundColor Yellow
    } else {
        Write-Host ("OK: {0,-52} channels={1}" -f $row.sensor_name,$channels.Count) -ForegroundColor Green
    }
}

if ($wrapperFailures.Count -gt 0) {
    Write-Host ""
    $wrapperFailures | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Final V11 fleet wrapper validation failed."
}

Write-Host ""
Write-Host "Fleet wrapper validation: PASS" -ForegroundColor Green
Write-Host "Maximum PRTG channels on any carrier: $maxChannels / 50"
Write-Host "Current real monitoring faults: $($realFaults.Count)"
$realFaults | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }

Write-Host ""
Write-Host "Validating 15-day SQLite history..."
& $Python $HistoryValidatorTarget
if ($LASTEXITCODE -ne 0) { throw "15-day history validation failed." }

Write-Host ""
Write-Host "Installing FINAL alarm/email policy task..."
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# One commissioning pass: deliberately re-arm current faults once, then send
# immediate alarm email(s) through the user's already configured email_notifier.
Write-Host ""
Write-Host "Sending one commissioning alarm email for each CURRENT fault..."
& $Python $AlarmTarget --rearm-current --once --strict
if ($LASTEXITCODE -ne 0) {
    throw "Commissioning alarm-email pass failed. Check logs\tv43_alarm_policy.log and email_notifier configuration."
}
Write-Host "Commissioning alarm-email pass: PASS" -ForegroundColor Green

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}" --interval 5' -f $AlarmTarget) `
    -WorkingDirectory $Project
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

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "FINAL V11 INSTALL PASS" -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Task state: $($task.State)"
Write-Host ""
Write-Host "FINAL ALERT POLICY:"
Write-Host "  1. Carrier UNLOCKED -> immediate ONE email; ALL multiplexed channels shown DOWN"
Write-Host "  2. Carrier LOCKED + TS DOWN -> immediate ONE email; ALL multiplexed channels shown DOWN"
Write-Host "  3. Carrier LOCKED + TS UP + individual service failure -> immediate email for ONLY failed service"
Write-Host "  4. No repeated emails while the same alarm remains active"
Write-Host "  5. Immediate recovery email when a notified alarm clears"
Write-Host "  6. Carrier/TS recovery -> ONE recovery email listing ALL recovered channels"
Write-Host "  7. Individual recovery -> recovered service only"
Write-Host "  8. Recovery includes alarm start, clear time, total downtime, and downtime on each recovered channel line"
Write-Host "  9. Red cross = DOWN; green check = UP"
Write-Host " 10. No 60-second debounce; no severity field added"
Write-Host ""
Write-Host "PRTG/HISTORY:"
Write-Host "  - Rich legacy-style technical parameters restored subject to 50-channel ceiling"
Write-Host "  - Full technical/service detail remains in sensor status text"
Write-Host "  - Full observations retained in SQLite for 15 days"
Write-Host "  - Existing 43 PRTG sensor objects were NOT recreated"
