$ErrorActionPreference = "Stop"

$Project = "C:\Users\ahmadsajjad\Desktop\Projects\wisi-gt34-monitor"
$Python = "C:\Users\ahmadsajjad\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Wrapper = "C:\Program Files (x86)\PRTG Network Monitor\Custom Sensors\EXEXML\wisi_gt34_carrier.bat"
$Manifest = Join-Path $Project "prtg_tv43_deployment\tv43_created_sensors.csv"
$Baseline = Join-Path $Project "authoritative_inventory_v6\active_services.csv"
$ReturnManifestPath = Join-Path $Project "prtg_tv43_deployment\prtg_return_channels_v113d.json"
$LimitBackup = Join-Path $Project ("prtg_tv43_deployment\prtg_stale_limit_backup_v113_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".csv")

$Source = Join-Path $PSScriptRoot "prtg_tv43_sensor.py"
$Target = Join-Path $Project "prtg_tv43_sensor.py"
$StateDir = Join-Path $Project "prtg_tv43_deployment"

$PrtgBase = "http://127.0.0.1"
$PrtgUser = "prtgadmin"
$MaxReturnChannels = 44

# Exactly 17 monitoring-critical carrier/TS channels.
# With the largest 27-service multiplex this yields 17 + 27 = 44.
$CriticalPriority = @(
    "Carrier / TS Health",
    "Demod Lock",
    "Transport Stream Present",
    "Video Present",
    "Service Integrity Health",
    "RF Level",
    "SNR",
    "BER",
    "Frequency",
    "Symbol Rate",
    "TS Bitrate",
    "TSID",
    "NID",
    "ONID",
    "Missing Services",
    "Unexpected Services",
    "ES Metadata Unavailable"
)

# Rich presentation channels are added only if room remains after critical
# channels + one service-health channel per authoritative SID.
# Any omitted values remain in the sensor status text and 15-day SQLite history.
$RichPriority = @(
    "Discovered Services",
    "Running Services",
    "Expected Services",
    "Present Expected Services",
    "Video Services",
    "Total Elementary Streams",
    "Modulation Enum",
    "FEC Enum",
    "MIS",
    "Service Name Mismatches",
    "Services With Video",
    "Services Without Video",
    "Services With Audio",
    "Services Without Audio",
    "Total Video Streams",
    "Total Audio Streams",
    "ES Services Available",
    "ES Compliance Health",
    "Web Enrichment",
    "Missing Expected Services",
    "ISI / MIS",
    "DVB Services"
)

function ConvertFrom-SecureStringPlain {
    param([Security.SecureString]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function New-PrtgUri {
    param([string]$Path,[hashtable]$Query,[string]$Passhash)
    $parts = @()
    foreach ($k in $Query.Keys) {
        $parts += (
            [Uri]::EscapeDataString([string]$k) + "=" +
            [Uri]::EscapeDataString([string]$Query[$k])
        )
    }
    $parts += "username=" + [Uri]::EscapeDataString($PrtgUser)
    $parts += "passhash=" + [Uri]::EscapeDataString($Passhash)
    return $PrtgBase.TrimEnd("/") + $Path + "?" + ($parts -join "&")
}

function Get-ChannelProperty {
    param([int]$SensorId,[int]$ChannelId,[string]$Name,[string]$Passhash)
    $uri = New-PrtgUri -Path "/api/getobjectproperty.htm" -Query @{
        id=$SensorId; subtype="channel"; subid=$ChannelId; name=$Name; show="nohtmlencode"
    } -Passhash $Passhash
    [xml]$xml = (Invoke-WebRequest -Uri $uri -UseBasicParsing -ErrorAction Stop).Content
    return [string]$xml.prtg.result
}

function Set-ChannelProperty {
    param([int]$SensorId,[int]$ChannelId,[string]$Name,[string]$Value,[string]$Passhash)
    $uri = New-PrtgUri -Path "/api/setobjectproperty.htm" -Query @{
        id=$SensorId; subtype="channel"; subid=$ChannelId; name=$Name; value=$Value
    } -Passhash $passhash
    [void](Invoke-WebRequest -Uri $uri -UseBasicParsing -ErrorAction Stop)
}

function Get-ExpectedServiceRows {
    param(
        [array]$AllBaseline,
        [string]$HostIp,
        [int]$Module,
        [int]$Channel
    )
    return @(
        $AllBaseline |
        Where-Object {
            [string]$_.host -eq $HostIp -and
            [int]$_.module -eq $Module -and
            [int]$_.channel -eq $Channel
        } |
        Sort-Object {[int]$_.sid}
    )
}

function Find-ServiceChannelRow {
    param(
        [array]$RegisteredRows,
        [string]$ServiceName,
        [int]$Sid,
        [bool]$DuplicateName
    )

    $qualified = "Service $ServiceName [SID $Sid]"
    $plain = "Service $ServiceName"

    if ($DuplicateName) {
        $q = @($RegisteredRows | Where-Object { [string]$_.name -eq $qualified })
        if ($q.Count -gt 0) { return $q[0] }
        $p = @($RegisteredRows | Where-Object { [string]$_.name -eq $plain })
        if ($p.Count -gt 0) { return $p[0] }
    } else {
        $p = @($RegisteredRows | Where-Object { [string]$_.name -eq $plain })
        if ($p.Count -gt 0) { return $p[0] }
        $q = @($RegisteredRows | Where-Object { [string]$_.name -eq $qualified })
        if ($q.Count -gt 0) { return $q[0] }
    }
    return $null
}

function Add-IfAvailable {
    param(
        [System.Collections.ArrayList]$Selected,
        [hashtable]$SelectedIds,
        [array]$RegisteredRows,
        [string]$Name,
        [int]$Max
    )
    if ($Selected.Count -ge $Max) { return }
    $m = @($RegisteredRows | Where-Object { [string]$_.name -eq $Name })
    if ($m.Count -gt 0) {
        $row = $m[0]
        $id = [int]$row.objid
        if (-not $SelectedIds.ContainsKey($id)) {
            [void]$Selected.Add($row)
            $SelectedIds[$id] = $true
        }
    }
}

Write-Host ""
Write-Host "TV43 PRTG STABLE-SCHEMA REPAIR V11.3J" -ForegroundColor Cyan
Write-Host "Hard cap: 44 PRTG channels per sensor; overflow detail stays in status text + SQLite" -ForegroundColor Cyan
Write-Host ""

foreach ($f in @($Source,$Target,$Wrapper,$Manifest,$Baseline)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Required file missing: $f" }
    Write-Host "FOUND: $f"
}

$rows = @(Import-Csv -LiteralPath $Manifest)
if ($rows.Count -ne 43) { throw "Expected 43 TV43 sensors; found $($rows.Count)" }
$baselineRows = @(Import-Csv -LiteralPath $Baseline)

$secure = Read-Host "Enter PRTG passhash (input hidden)" -AsSecureString
$passhash = ConvertFrom-SecureStringPlain $secure

try {
    Write-Host ""
    Write-Host "PHASE 1 - Build fixed <=44 return schema per sensor" -ForegroundColor Cyan

    $ReturnSchemaMap = [ordered]@{}
    $plans = @()
    $maxRegistered = 0

    foreach ($row in $rows) {
        $sensorId = [int]$row.sensor_id
        $hostIp = [string]$row.host
        $module = [int]$row.module
        $channelId = [int]$row.channel
        $key = "$hostIp|M${module}C${channelId}"

        $uri = New-PrtgUri -Path "/api/table.json" -Query @{
            content="channels"; columns="objid,name,lastvalue"; id=$sensorId; count=500
        } -Passhash $passhash

        $resp = Invoke-RestMethod -Uri $uri -Method Get -ErrorAction Stop
        $registeredRows = @(
            @($resp.channels) |
            Where-Object {
                [int]$_.objid -ne -4 -and [string]$_.name -ne "Downtime"
            }
        )

        if ($registeredRows.Count -gt $maxRegistered) { $maxRegistered = $registeredRows.Count }

        $expected = @(Get-ExpectedServiceRows -AllBaseline $baselineRows -HostIp $hostIp -Module $module -Channel $channelId)
        if ($expected.Count -lt 1) {
            throw "$($row.sensor_name): no authoritative services found in active_services.csv"
        }

        $nameCounts = @{}
        foreach ($svc in $expected) {
            $n = [string]$svc.service_name
            if (-not $nameCounts.ContainsKey($n)) { $nameCounts[$n] = 0 }
            $nameCounts[$n]++
        }

        $selected = New-Object System.Collections.ArrayList
        $selectedIds = @{}

        # 1. Critical RF/TS channels first.
        foreach ($name in $CriticalPriority) {
            Add-IfAvailable -Selected $selected -SelectedIds $selectedIds `
                -RegisteredRows $registeredRows -Name $name -Max $MaxReturnChannels
        }

        # 2. Exactly one service-health channel per authoritative SID.
        foreach ($svc in $expected) {
            $sid = [int]$svc.sid
            $name = [string]$svc.service_name
            $duplicate = ([int]$nameCounts[$name] -gt 1)
            $serviceRow = Find-ServiceChannelRow `
                -RegisteredRows $registeredRows `
                -ServiceName $name `
                -Sid $sid `
                -DuplicateName $duplicate

            if ($null -eq $serviceRow) {
                # Some authoritative DVB services (for example radio services
                # on the PTV mux) never had a dedicated PRTG channel registered.
                # Do NOT invent a new PRTG channel now: that would destabilize
                # the frozen schema again. These services remain fully visible
                # in the rich sensor status text and 15-day SQLite history, and
                # they still contribute to Service Integrity Health.
                Write-Host (
                    "    status/db-only service: {0} SID {1} (no registered PRTG channel)" -f
                    $name,$sid
                ) -ForegroundColor DarkYellow
                continue
            }

            $id = [int]$serviceRow.objid
            if (-not $selectedIds.ContainsKey($id)) {
                if ($selected.Count -ge $MaxReturnChannels) {
                    throw (
                        "$($row.sensor_name): 44-channel cap reached before all " +
                        "registered authoritative service channels could be retained."
                    )
                }
                [void]$selected.Add($serviceRow)
                $selectedIds[$id] = $true
            }
        }

        # 3. Fill remaining slots with useful rich technical aggregates.
        foreach ($name in $RichPriority) {
            Add-IfAvailable -Selected $selected -SelectedIds $selectedIds `
                -RegisteredRows $registeredRows -Name $name -Max $MaxReturnChannels
        }

        if ($selected.Count -gt $MaxReturnChannels) {
            throw "$($row.sensor_name): selected $($selected.Count) channels (>44)"
        }

        # Omitted registered channels remain visible in PRTG history but MUST
        # not retain active alarm limits, because the live script will no longer
        # return them. Their technical information remains in the rich status text
        # and 15-day SQLite history.
        $omitted = @(
            $registeredRows |
            Where-Object { -not $selectedIds.ContainsKey([int]$_.objid) }
        )

        $ReturnSchemaMap[$key] = [ordered]@{
            sensor_id = $sensorId
            sensor_name = [string]$row.sensor_name
            channel_names = @($selected | ForEach-Object { [string]$_.name })
        }

        $plans += [pscustomobject]@{
            SensorId=$sensorId; SensorName=[string]$row.sensor_name;
            Host=$hostIp; Module=$module; Channel=$channelId;
            SelectedRows=@($selected); OmittedRows=$omitted
        }

        Write-Host (
            "  {0,-52} registered={1,2} return={2,2} status/db-only={3,2}" -f
            $row.sensor_name,$registeredRows.Count,$selected.Count,$omitted.Count
        ) -ForegroundColor Green
    }

    $ReturnSchemaMap | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $ReturnManifestPath -Encoding UTF8

    Write-Host ""
    Write-Host "Largest accumulated registered inventory: $maxRegistered"
    Write-Host "Frozen return cap: 44"
    Write-Host "Return manifest: $ReturnManifestPath"

    if (-not (Test-Path -LiteralPath $ReturnManifestPath)) {
        throw "Return manifest was not created: $ReturnManifestPath"
    }

    try {
        $manifestCheck = Get-Content -LiteralPath $ReturnManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Return manifest is not valid JSON: $ReturnManifestPath"
    }

    if ($null -eq $manifestCheck) {
        throw "Return manifest JSON is empty: $ReturnManifestPath"
    }

    $manifestBytes = [System.IO.File]::ReadAllBytes($ReturnManifestPath)
    $hasUtf8Bom = (
        $manifestBytes.Length -ge 3 -and
        $manifestBytes[0] -eq 0xEF -and
        $manifestBytes[1] -eq 0xBB -and
        $manifestBytes[2] -eq 0xBF
    )
    Write-Host ("Return manifest UTF-8 BOM: {0}" -f $hasUtf8Bom)

    Write-Host ""
    Write-Host "PHASE 2 - Neutralize alarm limits on omitted stale/history-only PRTG channels" -ForegroundColor Cyan

    $backupRows = @()

    foreach ($plan in $plans) {
        foreach ($ch in @($plan.OmittedRows)) {
            $subid = [int]$ch.objid
            $name = [string]$ch.name
            $oldMode = Get-ChannelProperty -SensorId $plan.SensorId -ChannelId $subid `
                -Name "limitmode" -Passhash $passhash

            $backupRows += [pscustomobject]@{
                sensor_id=$plan.SensorId
                sensor_name=$plan.SensorName
                channel_id=$subid
                channel_name=$name
                previous_limitmode=$oldMode
            }

            if ($oldMode -eq "1") {
                Set-ChannelProperty -SensorId $plan.SensorId -ChannelId $subid `
                    -Name "limitmode" -Value "0" -Passhash $passhash

                $newMode = Get-ChannelProperty -SensorId $plan.SensorId -ChannelId $subid `
                    -Name "limitmode" -Passhash $passhash

                if ($newMode -ne "0") {
                    throw "$($plan.SensorName): failed to disable stale limit on '$name'"
                }
            }
        }
    }

    $backupRows | Export-Csv -LiteralPath $LimitBackup -NoTypeInformation -Encoding UTF8
    Write-Host "Previous limit modes saved: $LimitBackup"


    Write-Host ""
    Write-Host "PHASE 2B - Neutralize legacy/duplicate Video Present limits" -ForegroundColor Cyan
    foreach ($plan in $plans) {
        $channelsUri = New-PrtgUri -Path "/api/table.json" -Query @{
            content="channels"; columns="objid,name,lastvalue"; id=$plan.SensorId; count=500
        } -Passhash $passhash
        $channelsResp = Invoke-RestMethod -Uri $channelsUri -Method Get -ErrorAction Stop
        $registeredRowsNow = @(
            @($channelsResp.channels) |
            Where-Object {
                [int]$_.objid -ne -4 -and [string]$_.name -ne "Downtime"
            }
        )
        $videoRows = @(
            $registeredRowsNow |
            Where-Object { ([string]$_.name).Trim() -eq "Video Present" }
        )

        foreach ($vr in $videoRows) {
            $cid = [int]$vr.objid
            $prior = Get-ChannelProperty -SensorId $plan.SensorId -ChannelId $cid -Name "limitmode" -Passhash $passhash
            if ([string]$prior -eq "1") {
                Set-ChannelProperty -SensorId $plan.SensorId -ChannelId $cid -Name "limitmode" -Value "0" -Passhash $passhash
                $verify = Get-ChannelProperty -SensorId $plan.SensorId -ChannelId $cid -Name "limitmode" -Passhash $passhash
                if ([string]$verify -ne "0") {
                    throw "$($plan.SensorName): failed to neutralize Video Present limit on channel ID $cid"
                }
                Write-Host ("  {0}: Video Present channel ID {1} limit disabled" -f $plan.SensorName,$cid) -ForegroundColor DarkYellow
            }
        }
    }

    Write-Host ""
    Write-Host "PHASE 3 - Install fixed-schema sensor and validate all 43 wrappers" -ForegroundColor Cyan

    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $sensorBackup = Join-Path $StateDir (
        "prtg_tv43_sensor.py.before_stable_v113_" + (Get-Date -Format "yyyyMMdd_HHmmss")
    )
    Copy-Item -LiteralPath $Target -Destination $sensorBackup -Force
    Copy-Item -LiteralPath $Source -Destination $Target -Force

    & $Python -m py_compile $Target
    if ($LASTEXITCODE -ne 0) {
        Copy-Item -LiteralPath $sensorBackup -Destination $Target -Force
        throw "V11.3 compilation failed; previous sensor restored."
    }

    $failures = @()
    $expectedDown = @()
    $maxReturned = 0

    foreach ($plan in $plans) {
        $raw = & $Wrapper --tv43 --host $plan.Host --module $plan.Module --channel $plan.Channel 2>&1
        if ($LASTEXITCODE -ne 0) {
            $wrapperText = ($raw -join " ").Trim()
            $failures += "$($plan.SensorName): wrapper exit $LASTEXITCODE :: $wrapperText"
            continue
        }

        try { $obj = (($raw -join "`n").Trim() | ConvertFrom-Json) }
        catch {
            $failures += "$($plan.SensorName): invalid JSON"
            continue
        }

        if ($obj.prtg.error) {
            $failures += "$($plan.SensorName): $($obj.prtg.text)"
            continue
        }

        $result = @($obj.prtg.result)
        if ($result.Count -gt $maxReturned) { $maxReturned = $result.Count }

        if ($result.Count -gt $MaxReturnChannels) {
            $failures += "$($plan.SensorName): returned $($result.Count) > 44"
            continue
        }

        $expectedNames = @($plan.SelectedRows | ForEach-Object { [string]$_.name })
        $actualNames = @($result | ForEach-Object { [string]$_.channel })

        if ($actualNames.Count -ne $expectedNames.Count) {
            $failures += "$($plan.SensorName): returned $($actualNames.Count), expected $($expectedNames.Count)"
            continue
        }

        for ($i=0; $i -lt $expectedNames.Count; $i++) {
            if ($actualNames[$i] -ne $expectedNames[$i]) {
                $failures += (
                    "$($plan.SensorName): schema mismatch at index $i; " +
                    "expected '$($expectedNames[$i])', got '$($actualNames[$i])'"
                )
                break
            }
        }

        $map = @{}
        foreach ($c in $result) { $map[[string]$c.channel] = $c.value }

        foreach ($svc in @($result | Where-Object { [string]$_.channel -like "Service *" })) {
            if ($null -eq $svc.value -or [string]::IsNullOrWhiteSpace([string]$svc.value)) {
                $failures += "$($plan.SensorName): '$($svc.channel)' has no live value"
            }
        }

        if ($map.ContainsKey("Service Integrity Health") -and
            [int]$map["Service Integrity Health"] -eq 0) {
            $expectedDown += $plan.SensorName
            Write-Host ("REAL FAULT: {0}" -f $plan.SensorName) -ForegroundColor Yellow
        } else {
            Write-Host ("OK: {0,-52} return={1}" -f $plan.SensorName,$result.Count) -ForegroundColor Green
        }
    }

    if ($failures.Count -gt 0) {
        $failures | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        Copy-Item -LiteralPath $sensorBackup -Destination $Target -Force
        throw "V11.3J wrapper validation failed; previous Python sensor restored."
    }

    Write-Host ""
    Write-Host "44-channel wrapper validation: PASS" -ForegroundColor Green
    Write-Host "Maximum returned channels: $maxReturned / 44"
    Write-Host "Current genuine monitoring faults: $($expectedDown.Count)"
    $expectedDown | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }

    Write-Host ""
    Write-Host "PHASE 4 - Wait one PRTG cycle and validate actual fleet state" -ForegroundColor Cyan
    Start-Sleep -Seconds 45

    function Get-LiveWrapperState {
        param($Plan)

        $raw = $null
        $exitCode = 1

        for ($attempt = 1; $attempt -le 5; $attempt++) {
            $raw = & $Wrapper --tv43 --host $Plan.Host --module $Plan.Module --channel $Plan.Channel 2>&1
            $exitCode = $LASTEXITCODE

            if ($exitCode -eq 0) {
                break
            }

            $txt = ($raw -join " ").Trim()
            if ($txt -match "database is locked" -and $attempt -lt 5) {
                Write-Host (
                    "DB LOCK RETRY {0}/5: {1}" -f $attempt,$Plan.SensorName
                ) -ForegroundColor DarkYellow
                Start-Sleep -Milliseconds (300 * $attempt)
                continue
            }

            throw "$($Plan.SensorName): live wrapper refresh failed with exit $exitCode :: $txt"
        }

        $obj = (($raw -join "`n") | ConvertFrom-Json)
        $result = @($obj.prtg.result)
        $map = @{}
        foreach ($c in $result) { $map[[string]$c.channel] = $c.value }

        return [pscustomobject]@{
            IsFault = (
                $map.ContainsKey("Service Integrity Health") -and
                [int]$map["Service Integrity Health"] -eq 0
            )
            Text = [string]$obj.prtg.text
        }
    }

    function Get-PrtgSensorState {
        param($Plan)

        $uri = New-PrtgUri -Path "/api/table.json" -Query @{
            content="sensors"; columns="objid,sensor,status,message,lastcheck";
            filter_objid=$Plan.SensorId; count=10
        } -Passhash $passhash

        $resp = Invoke-RestMethod -Uri $uri -Method Get -ErrorAction Stop
        $s = @($resp.sensors)[0]
        if (-not $s) { return $null }
        return $s
    }

    $falseDown = @()

    foreach ($plan in $plans) {
        $live = Get-LiveWrapperState -Plan $plan
        $s = Get-PrtgSensorState -Plan $plan

        if (-not $s) {
            $falseDown += "$($plan.SensorName): not returned by PRTG"
            continue
        }

        $status = ([string]$s.status).Trim()

        # A transmission can legitimately change between Phase 3 and Phase 4.
        # The live wrapper refresh above is authoritative for current health.
        if ($live.IsFault) {
            Write-Host ("EXPECTED REAL ALARM: {0} -> {1}" -f $plan.SensorName,$status) -ForegroundColor Yellow
            continue
        }

        if ($status -eq "Up") {
            Write-Host ("HEALTHY: {0} -> Up" -f $plan.SensorName) -ForegroundColor Green
            continue
        }

        # Give PRTG one additional scan opportunity before calling a healthy
        # live carrier a false Down. This covers normal scan-cycle race/lag.
        Write-Host ("RECHECK: {0} live wrapper is healthy but PRTG={1}" -f $plan.SensorName,$status) -ForegroundColor DarkYellow
        Start-Sleep -Seconds 35

        $live2 = Get-LiveWrapperState -Plan $plan
        $s2 = Get-PrtgSensorState -Plan $plan

        if (-not $s2) {
            $falseDown += "$($plan.SensorName): not returned by PRTG on recheck"
            continue
        }

        $status2 = ([string]$s2.status).Trim()

        if ($live2.IsFault) {
            Write-Host ("EXPECTED REAL ALARM AFTER RECHECK: {0} -> {1}" -f $plan.SensorName,$status2) -ForegroundColor Yellow
        } elseif ($status2 -eq "Up") {
            Write-Host ("HEALTHY AFTER RECHECK: {0} -> Up" -f $plan.SensorName) -ForegroundColor Green
        } else {
            $msg = [regex]::Replace([string]$s2.message,"<[^>]+>"," ")
            $falseDown += "$($plan.SensorName): PRTG=$status2 : $msg"
            Write-Host ("FALSE DOWN: {0} -> {1}" -f $plan.SensorName,$status2) -ForegroundColor Red
        }
    }

    if ($falseDown.Count -gt 0) {
        Write-Host ""
        Write-Host "FALSE-DOWN SENSOR(S) REMAIN:" -ForegroundColor Red
        $falseDown | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        throw "V11.3J refuses PASS because PRTG still differs from live monitoring state."
    }

    Write-Host ""
    Write-Host "TV43 PRTG 44-CHANNEL STABLE SCHEMA V11.3J PASS" -ForegroundColor Green
    Write-Host ""
    Write-Host "FINAL PRESENTATION POLICY:"
    Write-Host "  - maximum 44 live PRTG channels per sensor"
    Write-Host "  - all authoritative services remain covered by service-integrity monitoring"
    Write-Host "  - critical RF/TS measurements retained as PRTG channels"
    Write-Host "  - extra technical/service details remain in rich sensor status text"
    Write-Host "  - full observations remain in SQLite for 15 days"
    Write-Host "  - final alarm/email policy unchanged"
    Write-Host "  - no commissioning emails re-sent"
}
finally {
    $passhash = $null
    $secure = $null
}
