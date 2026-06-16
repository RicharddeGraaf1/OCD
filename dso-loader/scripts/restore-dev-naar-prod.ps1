<#
.SYNOPSIS
  Runbook: volledige restore van de lokale dev-DB (localhost:5434/dso) naar de
  productie-Railway-PostGIS-DB. Brengt meteen het hernoemde `vth`-schema mee
  (geen aparte ALTER op prod) en ruimt het oude/partiële prod-schema op.

.DESCRIPTION
  Fase-gebaseerd en NIET-automatisch: je draait elke fase bewust. Destructieve
  fasen (Cleanup, Restore) vragen een typbevestiging tenzij -Force.

  Volgorde:
    1) -Dump      pg_dump van dev  -> lokaal dumpbestand (niet-destructief)
    2) -Cleanup   dropt app-schema's op PROD (DESTRUCTIEF)
    3) -Restore   pg_restore dump  -> PROD (DESTRUCTIEF/lang)
    4) -Refresh   herbouwt afgeleide data op PROD (locatie_subdiv + matviews)
    5) -Verify    telt rijen op PROD ter controle
  Of -All om 1..5 achter elkaar te draaien.

  PREREQUISITES (handmatig / go-moment, buiten dit script):
    - Railway PostGIS-volume vergroot naar >= ~100 GB (56 GB data + overhead).
    - Tijdelijke TCP-proxy aan op de PostGIS-service; gebruik die connectstring
      als -ProdUrl. Zet de proxy ná afloop weer uit.

.PARAMETER ProdUrl
  Volledige Postgres-connectstring naar PROD via de Railway TCP-proxy, bv.
  'postgresql://postgres:PW@maglev.proxy.rlwy.net:12345/railway'
  (Vereist voor Cleanup/Restore/Refresh/Verify.)

.PARAMETER DevUrl
  Connectstring naar de dev-DB. Default: DATABASE_URL uit c:\GIT\OCD\ocd-api\.env.

.EXAMPLE
  # Stap voor stap (aanbevolen):
  .\restore-dev-naar-prod.ps1 -Dump
  .\restore-dev-naar-prod.ps1 -Cleanup -ProdUrl '...'
  .\restore-dev-naar-prod.ps1 -Restore -ProdUrl '...'
  .\restore-dev-naar-prod.ps1 -Refresh -ProdUrl '...'
  .\restore-dev-naar-prod.ps1 -Verify  -ProdUrl '...'
#>
[CmdletBinding()]
param(
    [string]$ProdUrl,
    [string]$DevUrl,
    [string]$DumpFile = "c:\tmp\dso-prod-restore.dump",
    [string]$PgBin    = "C:\Program Files\PostgreSQL\17\bin",
    [string]$CleanupSql = "$PSScriptRoot\2026-06-prod-cleanup-before-restore.sql",
    [int]$Jobs = 4,
    [switch]$Dump,
    [switch]$Cleanup,
    [switch]$Restore,
    [switch]$Refresh,
    [switch]$Verify,
    [switch]$All,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$pg_dump    = Join-Path $PgBin 'pg_dump.exe'
$pg_restore = Join-Path $PgBin 'pg_restore.exe'
$psql       = Join-Path $PgBin 'psql.exe'

function Info($m){ Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m){   Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "[!] $m" -ForegroundColor Yellow }

foreach($exe in @($pg_dump,$pg_restore,$psql)){
    if(-not (Test-Path $exe)){ throw "PG17-client niet gevonden: $exe (pas -PgBin aan)" }
}

# Dev-URL bepalen
if(-not $DevUrl){
    $envFile = "c:\GIT\OCD\ocd-api\.env"
    if(Test-Path $envFile){
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*DATABASE_URL\s*=' } | Select-Object -First 1
        if($line){ $DevUrl = ($line -replace '^\s*DATABASE_URL\s*=\s*','').Trim().Trim('"').Trim("'") }
    }
    if(-not $DevUrl){ throw "Geen DevUrl en geen DATABASE_URL in $envFile" }
}

function Need-Prod(){ if(-not $ProdUrl){ throw "-ProdUrl is vereist voor deze fase (Railway TCP-proxy connectstring)." } }
function Confirm-Destructive($what){
    if($Force){ return }
    Warn "DESTRUCTIEF: $what"
    $ans = Read-Host "Typ exact 'RESTORE' om door te gaan"
    if($ans -ne 'RESTORE'){ throw "Afgebroken door gebruiker." }
}

if($All){ $Dump=$Cleanup=$Restore=$Refresh=$Verify=$true }
if(-not ($Dump -or $Cleanup -or $Restore -or $Refresh -or $Verify)){
    Warn "Geen fase gekozen. Gebruik -Dump / -Cleanup / -Restore / -Refresh / -Verify of -All. Zie -? voor help."
    return
}

# ---- 1. DUMP (niet-destructief) -------------------------------------------
if($Dump){
    Info "Dump dev-DB -> $DumpFile  (conv-schema uitgesloten = staging; locatie_subdiv-data uitgesloten = afgeleid, na restore herbouwd)"
    $dir = Split-Path $DumpFile -Parent
    if(-not (Test-Path $dir)){ New-Item -ItemType Directory -Path $dir | Out-Null }
    & $pg_dump -Fc -Z6 --no-owner --no-acl `
        --exclude-schema='conv' `
        --exclude-table-data='p2p.locatie_subdiv' `
        -d $DevUrl -f $DumpFile -v
    if($LASTEXITCODE -ne 0){ throw "pg_dump faalde (exit $LASTEXITCODE)" }
    $mb = [math]::Round((Get-Item $DumpFile).Length/1MB,1)
    Ok "Dump klaar: $DumpFile ($mb MB)"
}

# ---- 2. CLEANUP PROD (destructief) ----------------------------------------
if($Cleanup){
    Need-Prod
    Confirm-Destructive "drop alle app-schema's (incl. oude koop) op PROD via $CleanupSql"
    Info "Opschonen PROD..."
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $CleanupSql
    if($LASTEXITCODE -ne 0){ throw "cleanup faalde (exit $LASTEXITCODE)" }
    Ok "PROD opgeschoond."
}

# ---- 3. RESTORE PROD (destructief/lang) -----------------------------------
if($Restore){
    Need-Prod
    if(-not (Test-Path $DumpFile)){ throw "Dumpbestand ontbreekt: $DumpFile (draai eerst -Dump)" }
    Confirm-Destructive "pg_restore van $DumpFile naar PROD (kan uren duren)"
    Info "Restore naar PROD met $Jobs parallelle jobs... (extensie 'already exists'-meldingen zijn onschuldig)"
    # GEEN --exit-on-error: tolereer de PostGIS-extensie-die-al-bestaat meldingen.
    & $pg_restore --no-owner --no-acl --no-comments -j $Jobs -d $ProdUrl -v $DumpFile
    Warn "pg_restore exit-code: $LASTEXITCODE (een paar niet-fatale fouten bij extensies zijn normaal; controleer met -Verify)"
    Ok "Restore-fase afgerond."
}

# ---- 4. REFRESH afgeleide data --------------------------------------------
if($Refresh){
    Need-Prod
    Info "Afgeleide data herbouwen op PROD (locatie_subdiv + matviews)..."
    # NB1: refresh_locatie_subdiv() is een PYTHON-functie (loaders/subdiv.py), geen
    #      SQL-functie -> hier de onderliggende ST_Subdivide-INSERT inline.
    # NB2: parallelisme UIT — de Railway-container heeft een kleine /dev/shm; parallelle
    #      REFRESH/queries geven "could not resize shared memory segment / No space left".
    $sql = @'
SET max_parallel_workers_per_gather = 0;
SET max_parallel_maintenance_workers = 0;
SET work_mem = '256MB';
TRUNCATE p2p.locatie_subdiv;
INSERT INTO p2p.locatie_subdiv (identificatie, geometrie)
SELECT l.identificatie, ST_Subdivide(l.geometrie, 256)
FROM p2p.locatie l
WHERE ST_GeometryType(l.geometrie) IN ('ST_Polygon','ST_MultiPolygon');
DO $$
BEGIN
  IF to_regclass('p2p.naammatch_signaal') IS NOT NULL THEN
     EXECUTE 'REFRESH MATERIALIZED VIEW p2p.naammatch_signaal'; RAISE NOTICE 'refreshed p2p.naammatch_signaal';
  END IF;
  IF to_regclass('p2p.tekst_object_consistentie_mv') IS NOT NULL THEN
     EXECUTE 'REFRESH MATERIALIZED VIEW p2p.tekst_object_consistentie_mv'; RAISE NOTICE 'refreshed p2p.tekst_object_consistentie_mv';
  END IF;
  IF to_regclass('v2a.ponsenkaart_gemeente_stats') IS NOT NULL THEN
     EXECUTE 'REFRESH MATERIALIZED VIEW v2a.ponsenkaart_gemeente_stats'; RAISE NOTICE 'refreshed v2a.ponsenkaart_gemeente_stats';
  END IF;
END $$;
'@
    $tmp = Join-Path $env:TEMP 'ocd_refresh.sql'
    Set-Content -Path $tmp -Value $sql -Encoding utf8
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $tmp
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if($LASTEXITCODE -ne 0){ throw "refresh faalde (exit $LASTEXITCODE)" }
    Ok "Afgeleide data herbouwd."
}

# ---- 5. VERIFY ------------------------------------------------------------
if($Verify){
    Need-Prod
    Info "Verificatie op PROD..."
    $q = @'
SELECT 'schemas' AS check, string_agg(nspname,', ' ORDER BY nspname) AS value
  FROM pg_namespace WHERE nspname IN ('core','p2p','wro','i2a','v2a','vth')
UNION ALL SELECT 'vth.vergunningkennisgeving', count(*)::text FROM vth.vergunningkennisgeving
UNION ALL SELECT 'p2p.activiteit_locatieaanduiding', count(*)::text FROM p2p.activiteit_locatieaanduiding
UNION ALL SELECT 'p2p.tekst_element', count(*)::text FROM p2p.tekst_element
UNION ALL SELECT 'p2p.locatie_subdiv (herbouwd)', count(*)::text FROM p2p.locatie_subdiv
UNION ALL SELECT 'wro.planobject', count(*)::text FROM wro.planobject;
'@
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -c $q
    if($LASTEXITCODE -ne 0){ throw "verify faalde (exit $LASTEXITCODE)" }
    Ok "Verificatie klaar. Daarna: nieuwe code deployen + /v1/adres,/v1/zoek,/v1/vergunningen testen."
}
