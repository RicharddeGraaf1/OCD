<#
.SYNOPSIS
  Brengt de begrijpelijke-variant-cache (v2a.hertaling + koppeltabel) naar de
  Railway-prod-DB. Zwaar rekenwerk (het hertalen zelf) is al lokaal gedaan;
  dit script duwt alleen de kleine cache-tabel (~4 MB) en bouwt de koppel-MV
  server-side uit p2p.tekst_element dat al op prod staat.

.DESCRIPTION
  Fase-gebaseerd, bewust NIET volautomatisch. Spiegelt refresh-koop-to-prod.ps1.

    1) -Dump                   v2a.hertaling lokaal naar CSV exporteren. Geen prod.
    2) -Push  -ProdUrl <url>   base-DDL (schema/norm_hash/hertaling) + koppel-DDL
                               (mv_element_hash/element_hertaling) op prod, dan
                               de CSV idempotent upserten (ON CONFLICT).
    3) -Refresh -ProdUrl <url> REFRESH MATERIALIZED VIEW v2a.mv_element_hash op prod
                               (parallelisme uit i.v.m. krappe /dev/shm).
    4) -Verify  -ProdUrl <url> Tellingen: hertaling-rijen, mv-rijen, view-dekking.
  Of -All (vereist -ProdUrl) om 1..4 achter elkaar te draaien.

  PREREQUISITE voor Push/Refresh/Verify (handmatig go-moment):
    - Zet een TIJDELIJKE TCP-proxy aan op de PostGIS-service in Railway
      (dashboard: PostGIS service -> Settings -> Networking -> TCP Proxy).
      Gebruik die connectstring als -ProdUrl en zet de proxy ERNA weer UIT.
      (De publieke DB-proxy staat bewust dicht voor de veiligheid.)

  Waarom dit veilig herhaalbaar is:
    - base/koppel-DDL zijn idempotent (IF NOT EXISTS / OR REPLACE).
    - De upsert is op PK (bron_hash,model,prompt_versie) → dubbel draaien is safe.
    - De MV wordt server-side herbouwd uit p2p.tekst_element (188 MB op prod;
      niet door de leiding). Alleen de ~4 MB hertaling-tekst reist mee.

.PARAMETER ProdUrl
  Postgres-connectstring naar PROD via de tijdelijke Railway TCP-proxy, bv.
  'postgresql://postgres:PW@maglev.proxy.rlwy.net:12345/railway'.
#>
[CmdletBinding()]
param(
    [string]$ProdUrl,
    [string]$LocalUrl = "postgresql://postgres:postgres@localhost:5434/dso",
    [string]$ScriptsDir = "c:\GIT\OCD\dso-loader\scripts",
    [string]$PgBin     = "C:\Program Files\PostgreSQL\17\bin",
    [string]$WorkDir   = "c:\tmp",
    [switch]$Dump,
    [switch]$Push,
    [switch]$Refresh,
    [switch]$Verify,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$psql = Join-Path $PgBin 'psql.exe'

function Info($m){ Write-Host "[*] $m"  -ForegroundColor Cyan }
function Ok($m){   Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "[!] $m"  -ForegroundColor Yellow }

if(-not (Test-Path $psql)){ throw "PG17-psql niet gevonden: $psql (pas -PgBin aan)" }
if($All){ $Dump=$Push=$Refresh=$Verify=$true }
if(-not ($Dump -or $Push -or $Refresh -or $Verify)){
    Warn "Geen fase gekozen. Gebruik -Dump / -Push / -Refresh / -Verify of -All. Zie -? voor help."
    return
}
function Need-Prod(){ if(-not $ProdUrl){ throw "-ProdUrl is vereist voor deze fase (tijdelijke Railway TCP-proxy)." } }
if(-not (Test-Path $WorkDir)){ New-Item -ItemType Directory -Path $WorkDir | Out-Null }

$baseDdl = Join-Path $ScriptsDir '2026-07-hertaling-prod-base.sql'
$koppDdl = Join-Path $ScriptsDir '2026-07-add-element-hash-koppeling.sql'
$csv     = Join-Path $WorkDir   'hertaling_dump.csv'
$COLS    = 'bron_hash,model,prompt_versie,tekst,status,gegenereerd_op'

# ---- 1. DUMP LOKAAL -------------------------------------------------------
if($Dump){
    Info "v2a.hertaling exporteren naar $csv ..."
    & $psql $LocalUrl -v ON_ERROR_STOP=1 -c "\copy (SELECT $COLS FROM v2a.hertaling) TO '$csv' WITH (FORMAT csv, HEADER true)"
    if($LASTEXITCODE -ne 0){ throw "hertaling-export faalde (exit $LASTEXITCODE)" }
    $n  = (& $psql $LocalUrl -tAqc "SELECT count(*) FROM v2a.hertaling;").Trim()
    $mb = [math]::Round((Get-Item $csv).Length/1MB,2)
    Ok "Geexporteerd: $n rijen, $mb MB ($csv)."
}

# ---- 2. PUSH NAAR PROD ----------------------------------------------------
if($Push){
    Need-Prod
    if(-not (Test-Path $csv)){ throw "CSV niet gevonden ($csv). Draai eerst -Dump." }
    if(-not (Test-Path $baseDdl)){ throw "base-DDL niet gevonden: $baseDdl" }
    if(-not (Test-Path $koppDdl)){ throw "koppel-DDL niet gevonden: $koppDdl" }

    Info "Base-DDL op prod (schema / norm_hash / hertaling-tabel)..."
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $baseDdl | Out-Null
    if($LASTEXITCODE -ne 0){ throw "base-DDL op prod faalde (exit $LASTEXITCODE)" }
    Ok "Base-schema staat."

    # Idempotente upsert via temp-tabel -> INSERT ON CONFLICT DO UPDATE.
    Info "Hertaling-rijen upserten naar prod..."
    $importSql = @"
\set ON_ERROR_STOP on
CREATE TEMP TABLE _delta_h (LIKE v2a.hertaling INCLUDING DEFAULTS);
\copy _delta_h ($COLS) FROM '$csv' WITH (FORMAT csv, HEADER true)
INSERT INTO v2a.hertaling ($COLS)
SELECT $COLS FROM _delta_h
ON CONFLICT (bron_hash, model, prompt_versie)
DO UPDATE SET tekst=EXCLUDED.tekst, status=EXCLUDED.status, gegenereerd_op=EXCLUDED.gegenereerd_op;
"@
    $importFile = Join-Path $WorkDir 'hertaling_import.sql'
    Set-Content -Path $importFile -Value $importSql -Encoding utf8
    & $psql $ProdUrl -f $importFile
    if($LASTEXITCODE -ne 0){ throw "upsert op prod faalde (exit $LASTEXITCODE)" }
    Remove-Item $importFile -ErrorAction SilentlyContinue
    Ok "Hertaling-rijen ge-upsert."

    Info "Koppel-DDL op prod (mv_element_hash + element_hertaling, bouwt uit p2p)..."
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $koppDdl | Out-Null
    if($LASTEXITCODE -ne 0){ throw "koppel-DDL op prod faalde (exit $LASTEXITCODE)" }
    Ok "Koppeltabel + view staan (MV is bij CREATE meteen gevuld)."
}

# ---- 3. REFRESH MV OP PROD ------------------------------------------------
if($Refresh){
    Need-Prod
    Info "v2a.mv_element_hash verversen op prod (parallelisme uit i.v.m. kleine /dev/shm)..."
    $sql = @'
SET max_parallel_workers_per_gather = 0;
SET max_parallel_maintenance_workers = 0;
DO $$
BEGIN
  IF to_regclass('v2a.mv_element_hash') IS NULL THEN
    RAISE NOTICE 'mv_element_hash bestaat niet op prod - draai eerst -Push';
    RETURN;
  END IF;
  BEGIN
    EXECUTE 'REFRESH MATERIALIZED VIEW CONCURRENTLY v2a.mv_element_hash';
    RAISE NOTICE 'refreshed CONCURRENTLY';
  EXCEPTION WHEN OTHERS THEN
    EXECUTE 'REFRESH MATERIALIZED VIEW v2a.mv_element_hash';
    RAISE NOTICE 'refreshed (non-concurrent fallback)';
  END;
END $$;
'@
    $tmp = Join-Path $WorkDir 'hertaling_refresh.sql'
    Set-Content -Path $tmp -Value $sql -Encoding utf8
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $tmp
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if($LASTEXITCODE -ne 0){ throw "MV-refresh faalde (exit $LASTEXITCODE)" }
    Ok "MV ververst."
}

# ---- 4. VERIFY ------------------------------------------------------------
if($Verify){
    Need-Prod
    Info "Verificatie op prod..."
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -c @"
SELECT 'v2a.hertaling'         AS obj, count(*)::text AS n FROM v2a.hertaling
UNION ALL
SELECT 'v2a.mv_element_hash',       count(*)::text FROM v2a.mv_element_hash
UNION ALL
SELECT 'element_hertaling (join)',  count(*)::text FROM v2a.element_hertaling
UNION ALL
SELECT 'sonnet/v1 rijen',           count(*)::text FROM v2a.hertaling
  WHERE model='claude-sonnet-5' AND prompt_versie='v1';
"@
    if($LASTEXITCODE -ne 0){ throw "verify faalde (exit $LASTEXITCODE)" }
    Ok "Klaar. Vergeet de Railway TCP-proxy niet weer UIT te zetten."
}
