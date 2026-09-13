$ErrorActionPreference = "Stop"

$Project = "C:\Users\ahmadsajjad\Desktop\Projects\wisi-gt34-monitor"
$Python  = "C:\Users\ahmadsajjad\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Builder = Join-Path $PSScriptRoot "build_authoritative_inventory_v6.py"

$ExistingRaw = Join-Path $Project "authoritative_inventory_v5\raw"
$Work        = Join-Path $Project "authoritative_inventory_v6"

if (-not (Test-Path -LiteralPath $Builder)) {
    throw "Builder not found: $Builder"
}

if (-not (Test-Path -LiteralPath $ExistingRaw)) {
    throw "Existing V5 raw discovery folder not found: $ExistingRaw"
}

$rawFiles = @(Get-ChildItem -LiteralPath $ExistingRaw -Filter "services_*.txt" -File)
if ($rawFiles.Count -ne 4) {
    throw "Expected exactly 4 existing raw service discovery files; found $($rawFiles.Count)."
}

New-Item -ItemType Directory -Force -Path $Work | Out-Null

Write-Host "Using existing V5 raw discovery files (NO NEW WISI DISCOVERY)..."
$rawFiles | Select-Object Name,Length | Format-Table -AutoSize

Write-Host ""
Write-Host "Building structured authoritative inventory with corrected V6 parser..."
& $Python $Builder `
    --input-dir $ExistingRaw `
    --output-dir $Work `
    --expect-hosts "192.168.3.27" "192.168.3.45" "192.168.3.8" "192.168.3.9"

if ($LASTEXITCODE -ne 0) {
    throw "Structured inventory V6 validation failed."
}

Write-Host ""
Write-Host "======================================================================="
Write-Host "AUTHORITATIVE INVENTORY V6 PASS"
Write-Host "======================================================================="
Write-Host "Folder: $Work"
Write-Host "NO PRTG OBJECTS OR PRODUCTION MONITOR FILES WERE MODIFIED."
