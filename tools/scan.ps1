param(
  [Parameter(Mandatory=$true)][string]$Asar,
  [Parameter(Mandatory=$true)][string]$Pattern,
  [int]$MaxHits = 40,
  [int]$Context = 200
)

$ErrorActionPreference = 'Stop'

# ---- header ----
$fs = [System.IO.File]::OpenRead($Asar)
$hdr = New-Object byte[] 16
$null = $fs.Read($hdr, 0, 16)
$headerSize = [BitConverter]::ToUInt32($hdr, 4)
$jsonLen    = [BitConverter]::ToUInt32($hdr, 12)
$dataOffset = 8 + $headerSize

$buf = New-Object byte[] $jsonLen
$fs.Position = 16
$read = 0
while ($read -lt $jsonLen) {
  $n = $fs.Read($buf, $read, $jsonLen - $read)
  if ($n -le 0) { break }
  $read += $n
}
$root = ([System.Text.Encoding]::UTF8.GetString($buf, 0, $read)) | ConvertFrom-Json

# ---- flatten into sorted ranges ----
$list = New-Object System.Collections.Generic.List[object]
function Walk($node, $prefix) {
  foreach ($p in $node.files.PSObject.Properties) {
    $child = $p.Value
    $full = if ($prefix) { "$prefix/$($p.Name)" } else { $p.Name }
    if ($child.PSObject.Properties.Name -contains 'files') { Walk $child $full }
    else {
      $list.Add([pscustomobject]@{
        Name  = $full
        Start = $dataOffset + [int64]$child.offset
        End   = $dataOffset + [int64]$child.offset + [int64]$child.size - 1
        Size  = [int64]$child.size
      })
    }
  }
}
Walk $root ''
$ranges = $list | Sort-Object Start | Select-Object -ExpandProperty PSObject -ErrorAction SilentlyContinue
$sorted = @($list | Sort-Object Start)

function Resolve-Name([int64]$pos) {
  $lo = 0; $hi = $sorted.Count - 1; $best = $null
  while ($lo -le $hi) {
    $mid = [int](($lo + $hi) / 2)
    $e = $sorted[$mid]
    if ($e.Start -le $pos) { $best = $e; $lo = $mid + 1 } else { $hi = $mid - 1 }
  }
  if ($best -and $pos -le $best.End) { return $best }
  return $null
}

# ---- chunked latin-1 scan ----
$latin = [System.Text.Encoding]::GetEncoding(28591)
$total = $fs.Length
$chunk = 16MB
$overlap = 512
$hits = 0
$pos = 0
$tail = ''
$tailStart = 0

while ($pos -lt $total -and $hits -lt $MaxHits) {
  $len = [Math]::Min($chunk, $total - $pos)
  $fs.Position = $pos
  $b = New-Object byte[] $len
  $got = 0
  while ($got -lt $len) {
    $n = $fs.Read($b, $got, $len - $got)
    if ($n -le 0) { break }
    $got += $n
  }
  $text = $tail + $latin.GetString($b, 0, $got)
  $base = $tailStart
  $idx = 0
  while ($true) {
    $idx = $text.IndexOf($Pattern, $idx, [System.StringComparison]::Ordinal)
    if ($idx -lt 0) { break }
    $abs = $base + $idx
    $e = Resolve-Name $abs
    $name = if ($e) { $e.Name } else { '<unmapped>' }
    $s = [Math]::Max(0, $idx - $Context)
    $l = [Math]::Min($Context * 2, $text.Length - $s)
    $snippet = $text.Substring($s, $l) -replace '[^\x20-\x7E]', '.'
    Write-Host "--- hit @ $abs  in  $name"
    Write-Host "    $snippet"
    $hits++
    if ($hits -ge $MaxHits) { break }
    $idx += $Pattern.Length
  }
  $tailStart = $pos + $got - $overlap
  $tail = if ($tailStart -ge 0) { $latin.GetString($b, [Math]::Max(0, $got - $overlap), [Math]::Min($overlap, $got)) } else { '' }
  $pos += $got
}

$fs.Close()
Write-Host "done. hits=$hits"
