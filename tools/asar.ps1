param(
  [Parameter(Mandatory=$true)][string]$Asar,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [string[]]$Filter = @('*'),
  [switch]$ListOnly
)

$ErrorActionPreference = 'Stop'

# --- read header ---
$fs = [System.IO.File]::OpenRead($Asar)
try {
  $hdr = New-Object byte[] 16
  $null = $fs.Read($hdr, 0, 16)
  $headerSize = [BitConverter]::ToUInt32($hdr, 4)
  $jsonLen    = [BitConverter]::ToUInt32($hdr, 12)
  $jsonStart  = 16
  $dataOffset = 8 + $headerSize

  $buf = New-Object byte[] $jsonLen
  $fs.Position = $jsonStart
  $read = 0
  while ($read -lt $jsonLen) {
    $n = $fs.Read($buf, $read, $jsonLen - $read)
    if ($n -le 0) { break }
    $read += $n
  }
  $json = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read).TrimEnd([char]0, ' ')
  $root = $json | ConvertFrom-Json

  # --- flatten ---
  $entries = New-Object System.Collections.Generic.List[object]
  function Walk($node, $prefix) {
    foreach ($p in $node.files.PSObject.Properties) {
      $child = $p.Value
      $full = if ($prefix) { "$prefix/$($p.Name)" } else { $p.Name }
      if ($child.PSObject.Properties.Name -contains 'files') {
        Walk $child $full
      } else {
        $entries.Add([pscustomobject]@{
          Name   = $full
          Offset = [int64]$child.offset
          Size   = [int64]$child.size
        })
      }
    }
  }
  Walk $root ''

  Write-Host "asar entries: $($entries.Count)  dataOffset: $dataOffset"

  $hits = $entries | Where-Object {
    $n = $_.Name
    $matched = $false
    foreach ($f in $Filter) {
      $rx = '^' + [regex]::Escape($f).Replace('\*', '.*').Replace('\?', '.') + '$'
      if ($n -match $rx) { $matched = $true; break }
    }
    $matched
  }

  Write-Host "matched: $($hits.Count)"

  if ($ListOnly) {
    $hits | ForEach-Object { Write-Host ("  {0}  ({1} bytes)" -f $_.Name, $_.Size) }
    return
  }

  foreach ($e in $hits) {
    $target = Join-Path $OutDir ($e.Name -replace '/', '\')
    $tdir = Split-Path $target -Parent
    if (-not (Test-Path $tdir)) { New-Item -ItemType Directory -Path $tdir -Force | Out-Null }
    $fs.Position = $dataOffset + $e.Offset
    $ob = New-Object byte[] $e.Size
    $got = 0
    while ($got -lt $e.Size) {
      $n = $fs.Read($ob, $got, $e.Size - $got)
      if ($n -le 0) { break }
      $got += $n
    }
    [System.IO.File]::WriteAllBytes($target, $ob)
    Write-Host ("  {0}  ({1} bytes)" -f $e.Name, $e.Size)
  }
}
finally { $fs.Close() }
