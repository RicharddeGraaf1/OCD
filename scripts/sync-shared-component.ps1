<#
.SYNOPSIS
  Synchroniseert de gedeelde <ocd-regeltekst>-component vanuit de canonieke bron
  naar alle consument-repositories.

.DESCRIPTION
  Canonieke bron : OCD/ocd-api/assets/ocd-regeltekst.v1.js  (de ENIGE die je bewerkt)
  Doelen         : de gevendorde kopie in elke consument-repo.

  Elke kopie wordt byte-voor-byte overschreven met de canonieke inhoud en daarna
  op SHA256 geverifieerd. Zo is drift onmogelijk zolang je nooit een kopie met de
  hand bewerkt maar altijd hier wijzigt + dit script draait.

.PARAMETER Check
  Niets kopieren, alleen verifieren of elke kopie gelijk is aan canoniek.
  Exit-code 1 bij drift. Bedoeld voor een pre-commit / CI-guard.

.EXAMPLE
  pwsh OCD/scripts/sync-shared-component.ps1
  pwsh OCD/scripts/sync-shared-component.ps1 -Check
#>
[CmdletBinding()]
param([switch]$Check)

$ErrorActionPreference = 'Stop'

# Canoniek = ../ocd-api/assets/... t.o.v. dit script; GIT-root = ../.. t.o.v. dit script.
$canonical = Join-Path $PSScriptRoot '..\ocd-api\assets\ocd-regeltekst.v1.js' | Resolve-Path
$gitRoot   = Join-Path $PSScriptRoot '..\..' | Resolve-Path

# Doel-paden relatief aan de GIT-root (c:\GIT).
$targets = @(
  'RoM-prototype\lib\ocd-regeltekst.js'
  'instructieregels.nl\web\ocd-regeltekst.js'
  'OCDviewer\frontend\src\app\features\viewer\components\document-leestekst\ocd-regeltekst.js'
  'omgevingsbot.nl\frontend\public\ocd-regeltekst.js'
)

$canonHash = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash
Write-Host "Canoniek : $canonical" -ForegroundColor Cyan
Write-Host "SHA256   : $canonHash`n"

$mode = if ($Check) { 'CHECK (alleen verifieren)' } else { 'SYNC (kopieren + verifieren)' }
Write-Host "Modus    : $mode`n" -ForegroundColor Cyan

$drift = 0
$missing = 0
foreach ($rel in $targets) {
  $dst    = Join-Path $gitRoot $rel
  $dstDir = Split-Path $dst -Parent

  if (-not (Test-Path -LiteralPath $dstDir)) {
    Write-Host ("  ONTBREEKT-MAP  {0}  (map bestaat niet - repo niet uitgecheckt?)" -f $rel) -ForegroundColor Yellow
    $missing++
    continue
  }

  if (-not $Check) {
    Copy-Item -LiteralPath $canonical -Destination $dst -Force
  }

  if (-not (Test-Path -LiteralPath $dst)) {
    Write-Host ("  GEEN-KOPIE     {0}  (kopie ontbreekt - draai zonder -Check om aan te maken)" -f $rel) -ForegroundColor Yellow
    $drift++
    continue
  }

  $dstHash = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash
  if ($dstHash -eq $canonHash) {
    $verb = if ($Check) { 'GELIJK' } else { 'GESYNCT' }
    Write-Host ("  {0,-14} {1}" -f $verb, $rel) -ForegroundColor Green
  } else {
    Write-Host ("  DRIFT          {0}" -f $rel) -ForegroundColor Red
    $drift++
  }
}

Write-Host ''
if ($missing -gt 0) {
  Write-Host "$missing doel-map(pen) ontbreekt - overgeslagen." -ForegroundColor Yellow
}
if ($drift -gt 0) {
  if ($Check) {
    Write-Host "DRIFT gevonden in $drift kopie(en). Draai dit script zonder -Check om te synchroniseren." -ForegroundColor Red
  } else {
    Write-Host "$drift kopie(en) konden niet worden gesynct." -ForegroundColor Red
  }
  exit 1
}
Write-Host "Alle bereikbare kopieen zijn gelijk aan canoniek." -ForegroundColor Green
