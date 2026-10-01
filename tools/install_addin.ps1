# Installs the Fusion DSH Bridge add-in into Fusion 360's own AddIns folder
# and enables runOnStartup, so Fusion loads it automatically.
#
# The workspace copy stays the source of truth; re-run this after editing it.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1

param(
  [string]$Source,
  [string]$Target,
  [switch]$NoAutoStart
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Source) { $Source = Join-Path $repoRoot 'fusion_addin' }
if (-not $Target) {
  $Target = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns\FusionDSHBridge'
}

if (-not (Test-Path (Join-Path $Source 'FusionDSHBridge.manifest'))) {
  throw "no FusionDSHBridge.manifest under $Source"
}

Write-Host "source : $Source"
Write-Host "target : $Target"

New-Item -ItemType Directory -Path $Target -Force | Out-Null

# --- code and config ---
foreach ($name in @('FusionDSHBridge.py', 'bridge_config.json')) {
  $from = Join-Path $Source $name
  if (Test-Path $from) {
    Copy-Item $from (Join-Path $Target $name) -Force
    Write-Host "  copied $name"
  }
}

# --- manifest, with runOnStartup flipped ---
$manifestText = [System.IO.File]::ReadAllText((Join-Path $Source 'FusionDSHBridge.manifest'))
if ($NoAutoStart) {
  $manifestText = $manifestText -replace '"runOnStartup"\s*:\s*(true|false)', '"runOnStartup": false'
} else {
  $manifestText = $manifestText -replace '"runOnStartup"\s*:\s*(true|false)', '"runOnStartup": true'
}

# Fusion parses this as JSON: write UTF-8 with no BOM, or it will not load.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $Target 'FusionDSHBridge.manifest'), $manifestText, $utf8NoBom)
Write-Host "  wrote FusionDSHBridge.manifest (runOnStartup = $(-not $NoAutoStart))"

# --- verify ---
$installed = Get-ChildItem $Target -File | Select-Object Name, Length
Write-Host ""
Write-Host "installed:"
$installed | ForEach-Object { Write-Host ("  {0,8:N0}  {1}" -f $_.Length, $_.Name) }

$final = [System.IO.File]::ReadAllText((Join-Path $Target 'FusionDSHBridge.manifest'))
# Windows PowerShell's ConvertFrom-Json rejects the empty-string key Autodesk
# puts inside "description", so check the fields by pattern instead.
Write-Host ""
Write-Host "manifest checks:"
foreach ($field in @('type', 'id', 'version')) {
  $value = [regex]::Match($final, '"' + $field + '"\s*:\s*"([^"]*)"').Groups[1].Value
  Write-Host ("  {0,-13}: {1}" -f $field, $value)
}
$autoStart = [regex]::Match($final, '"runOnStartup"\s*:\s*(\w+)').Groups[1].Value
Write-Host ("  {0,-13}: {1}" -f 'runOnStartup', $autoStart)
$balanced = ([regex]::Matches($final, '\{').Count -eq [regex]::Matches($final, '\}').Count)
Write-Host ("  {0,-13}: {1}" -f 'braces', $(if ($balanced) { 'balanced' } else { 'UNBALANCED' }))
$bom = [System.IO.File]::ReadAllBytes((Join-Path $Target 'FusionDSHBridge.manifest'))[0..2]
Write-Host ("  {0,-13}: {1}" -f 'BOM', $(if ($bom[0] -eq 0xEF) { 'PRESENT - Fusion may reject this' } else { 'none' }))
