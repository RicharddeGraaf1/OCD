<#
.SYNOPSIS
  Wekelijkse verse-data-cyclus voor omgevingsvergunningenregister.nl.
  Laadt KOOP-kennisgevingen LOKAAL (zwaar werk op je eigen machine) en duwt
  daarna alleen de NIEUWE/gewijzigde rijen (delta) naar de Railway-prod-DB.
  Houdt de krappe Railway-PostGIS lean: geen fetch/enrich/RAM-piek op de server.

.DESCRIPTION
  Fase-gebaseerd en bewust NIET volautomatisch. Draai per week:

    1) -LoadLocal              KOOP incrementeel bijladen in de lokale dev-DB
                               (load-koop + enrich-koop). Geen prod nodig.
    2) -Push  -ProdUrl <url>   Delta (rijen vanaf de prod-watermark) upserten
                               in vth.vergunningkennisgeving op PROD.
    3) -Refresh -ProdUrl <url> VACUUM (ANALYZE) op vth.vergunningkennisgeving
                               (verplicht: vult de visibility map, zonder dat
                               vallen /stats, /facets en /pins in de 20s
                               statement_timeout) + matviews verversen:
                               vth.dossier_doorlooptijd, vth.vergunning_stats,
                               vth.vergunning_stats_type_besluit.
    4) -Verify  -ProdUrl <url> Telling + max(datum_publicatie) op PROD.
  Of -All (vereist -ProdUrl) om 1..4 achter elkaar te draaien.

  PREREQUISITE voor Push/Refresh/Verify (handmatig go-moment):
    - Zet een TIJDELIJKE TCP-proxy aan op de PostGIS-service in Railway
      (dashboard: PostGIS service -> Settings -> Networking -> TCP Proxy).
      Gebruik die connectstring als -ProdUrl en zet de proxy ERNA weer UIT.
      (De publieke DB-proxy staat bewust dicht voor de veiligheid.)

  Waarom delta i.p.v. volledige restore (zie restore-dev-naar-prod.ps1):
    - Volledige 5GB-restore duurt ~30 min en is elke week overkill.
    - Upsert op PK koop_id is idempotent: veilig om overlappende dagen mee te
      sturen. De watermark (prod max datum_publicatie - overlap) bepaalt de omvang;
      eerste run haalt de hele achterstand in, daarna ~een paar duizend rijen/week.

.PARAMETER ProdUrl
  Postgres-connectstring naar PROD via de tijdelijke Railway TCP-proxy, bv.
  'postgresql://postgres:PW@maglev.proxy.rlwy.net:12345/railway'.

.PARAMETER Since
  Optioneel: forceer de delta-ondergrens (YYYY-MM-DD). Zonder deze param leidt
  het script de watermark af uit PROD zelf: max(datum_publicatie) - OverlapDays.

.PARAMETER OverlapDays
  Dagen die de delta terug-overlapt om na-publicaties/verrijkingen mee te nemen.
  Default 3. Upsert maakt dubbele dagen onschadelijk.

.EXAMPLE
  # Wekelijks, stap voor stap (aanbevolen):
  .\refresh-koop-to-prod.ps1 -LoadLocal
  .\refresh-koop-to-prod.ps1 -Push    -ProdUrl 'postgresql://...'
  .\refresh-koop-to-prod.ps1 -Refresh -ProdUrl 'postgresql://...'
  .\refresh-koop-to-prod.ps1 -Verify  -ProdUrl 'postgresql://...'

.EXAMPLE
  # Eerste inhaalslag (prod staat op 19 mei): forceer de ondergrens.
  .\refresh-koop-to-prod.ps1 -Push -ProdUrl 'postgresql://...' -Since 2026-05-16
#>
[CmdletBinding()]
param(
    [string]$ProdUrl,
    [string]$Since,
    [int]$OverlapDays = 3,
    [string]$LocalUrl = "postgresql://postgres:postgres@localhost:5434/dso",
    [string]$LoaderDir = "c:\GIT\OCD\dso-loader",
    [string]$PgBin     = "C:\Program Files\PostgreSQL\17\bin",
    [string]$WorkDir   = "c:\tmp",
    [switch]$LoadLocal,
    [switch]$Push,
    [switch]$Refresh,
    [switch]$Verify,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$psql   = Join-Path $PgBin 'psql.exe'
$python = Join-Path $LoaderDir '.venv\Scripts\python.exe'

function Info($m){ Write-Host "[*] $m"  -ForegroundColor Cyan }
function Ok($m){   Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "[!] $m"  -ForegroundColor Yellow }

if(-not (Test-Path $psql))   { throw "PG17-psql niet gevonden: $psql (pas -PgBin aan)" }
if($All){ $LoadLocal=$Push=$Refresh=$Verify=$true }
if(-not ($LoadLocal -or $Push -or $Refresh -or $Verify)){
    Warn "Geen fase gekozen. Gebruik -LoadLocal / -Push / -Refresh / -Verify of -All. Zie -? voor help."
    return
}
function Need-Prod(){ if(-not $ProdUrl){ throw "-ProdUrl is vereist voor deze fase (tijdelijke Railway TCP-proxy)." } }
if(-not (Test-Path $WorkDir)){ New-Item -ItemType Directory -Path $WorkDir | Out-Null }

# Kolomnaam van de PK; blijft bij één plek zodat de queries kloppen.
$TABLE = 'vth.vergunningkennisgeving'
$PK    = 'koop_id'

# ---- 1. LOAD LOCAL --------------------------------------------------------
if($LoadLocal){
    if(-not (Test-Path $python)){ throw "Loader-venv niet gevonden: $python" }
    # Ondergrens = EERSTE ontbrekende/niet-ok dag sinds Ow-ingang (niet max(ok):
    # een out-of-order load kan max(ok) vóór een gat zetten, waardoor het gat
    # onterecht wordt overgeslagen). load-koop skipt tussenliggende ok-dagen zelf,
    # dus dit vult gaten én nieuwe dagen zonder dubbel werk. Geen gaten meer →
    # eerste ontbrekende dag = morgen → lege range (niets te doen).
    $fromDate = & $psql $LocalUrl -tAqc @"
WITH days AS (SELECT generate_series(DATE '2024-01-01', CURRENT_DATE, '1 day')::date d),
ok AS (SELECT processed_date FROM vth.etl_run
        WHERE source='koop_omgevingsvergunning' AND status='ok')
SELECT to_char(
  COALESCE(min(d.d) FILTER (WHERE ok.processed_date IS NULL), CURRENT_DATE + 1),
  'YYYY-MM-DD')
FROM days d LEFT JOIN ok ON ok.processed_date = d.d;
"@
    if($LASTEXITCODE -ne 0){ throw "kon lokale etl_run-watermark niet lezen" }
    $toDate = (Get-Date).ToString('yyyy-MM-dd')
    Info "Lokaal bijladen: load-koop --from $fromDate --to $toDate"
    Push-Location $LoaderDir
    try {
        & $python -m src.cli load-koop --from $fromDate --to $toDate
        if($LASTEXITCODE -ne 0){ throw "load-koop faalde (exit $LASTEXITCODE)" }
        Info "Verrijken: enrich-koop --loop --stop-after-empty 3"
        & $python -m src.cli enrich-koop --loop --stop-after-empty 3
        if($LASTEXITCODE -ne 0){ throw "enrich-koop faalde (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
    $n = & $psql $LocalUrl -tAqc "SELECT count(*)||' records, max '||max(datum_publicatie) FROM $TABLE;"
    Ok "Lokale DB bijgewerkt: $n"
}

# ---- 2. PUSH DELTA NAAR PROD ---------------------------------------------
if($Push){
    Need-Prod
    # Zorg dat het prod-schema de (idempotente) vth-kolommen/indexen heeft — vangt
    # kolom-drift af zodat de kolom-doorsnede hieronder zo breed mogelijk is.
    Info "Prod vth-schema idempotent bijwerken (KOOP_DDL)..."
    $ddlFile = Join-Path $WorkDir 'koop_ddl.sql'
    Push-Location $LoaderDir
    try { & $python -c "from src.ddl import KOOP_DDL; import sys; sys.stdout.write(KOOP_DDL)" | Out-File -Encoding utf8 $ddlFile }
    finally { Pop-Location }
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $ddlFile | Out-Null
    if($LASTEXITCODE -ne 0){ throw "KOOP_DDL op prod faalde (exit $LASTEXITCODE)" }
    Ok "Prod-schema bij."

    # Watermark bepalen.
    if($Since){
        $since = $Since
        Info "Delta-ondergrens (opgegeven): $since"
    } else {
        $since = & $psql $ProdUrl -tAqc @"
SELECT to_char(COALESCE(max(datum_publicatie), DATE '2024-01-01') - $OverlapDays, 'YYYY-MM-DD')
FROM $TABLE;
"@
        if($LASTEXITCODE -ne 0){ throw "kon prod-watermark niet lezen" }
        Info "Delta-ondergrens (afgeleid van prod max - $OverlapDays d): $since"
    }

    # Gedeelde kolommen (doorsnede lokaal ∩ prod), in lokale kolomvolgorde.
    $colQuery = @"
SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
FROM information_schema.columns
WHERE table_schema='vth' AND table_name='vergunningkennisgeving'
  AND column_name IN (%LIST%);
"@
    $prodCols = & $psql $ProdUrl -tAqc "SELECT string_agg(quote_literal(column_name), ',') FROM information_schema.columns WHERE table_schema='vth' AND table_name='vergunningkennisgeving';"
    if([string]::IsNullOrWhiteSpace($prodCols)){ throw "geen kolommen gevonden op prod (bestaat de tabel?)" }
    $cols = & $psql $LocalUrl -tAqc ($colQuery -replace '%LIST%', $prodCols)
    if([string]::IsNullOrWhiteSpace($cols)){ throw "kon gedeelde kolommen niet bepalen" }
    $colList = $cols.Trim()
    Info "Gedeelde kolommen: $colList"

    # SET-clause voor de upsert (alle gedeelde kolommen behalve de PK).
    $setClause = & $psql $LocalUrl -tAqc @"
SELECT string_agg(format('%I=EXCLUDED.%I', column_name, column_name), ', ' ORDER BY ordinal_position)
FROM information_schema.columns
WHERE table_schema='vth' AND table_name='vergunningkennisgeving'
  AND column_name IN ($prodCols) AND column_name <> '$PK';
"@
    $setClause = $setClause.Trim()

    # Delta lokaal exporteren (CSV met expliciete kolomlijst).
    $deltaFile = Join-Path $WorkDir 'koop_delta.csv'
    Info "Delta exporteren uit lokaal (datum_publicatie >= $since)..."
    & $psql $LocalUrl -v ON_ERROR_STOP=1 -c "\copy (SELECT $colList FROM $TABLE WHERE datum_publicatie >= '$since') TO '$deltaFile' WITH (FORMAT csv, HEADER true)"
    if($LASTEXITCODE -ne 0){ throw "delta-export faalde (exit $LASTEXITCODE)" }
    $nDelta = (& $psql $LocalUrl -tAqc "SELECT count(*) FROM $TABLE WHERE datum_publicatie >= '$since';").Trim()
    $mb = [math]::Round((Get-Item $deltaFile).Length/1MB,1)
    Info "Delta: $nDelta rijen, $mb MB."

    # Upsert op prod in één sessie (temp-tabel -> INSERT ON CONFLICT).
    $importSql = @"
\set ON_ERROR_STOP on
CREATE TEMP TABLE _delta_vk (LIKE $TABLE INCLUDING DEFAULTS);
\copy _delta_vk ($colList) FROM '$deltaFile' WITH (FORMAT csv, HEADER true)
INSERT INTO $TABLE ($colList)
SELECT $colList FROM _delta_vk
ON CONFLICT ($PK) DO UPDATE SET $setClause;
"@
    $importFile = Join-Path $WorkDir 'koop_import.sql'
    Set-Content -Path $importFile -Value $importSql -Encoding utf8
    Info "Upserten naar prod..."
    & $psql $ProdUrl -f $importFile
    if($LASTEXITCODE -ne 0){ throw "upsert op prod faalde (exit $LASTEXITCODE)" }
    Remove-Item $importFile -ErrorAction SilentlyContinue
    Ok "Delta ge-upsert op prod ($nDelta rijen aangeboden)."
}

# ---- 3. VACUUM + MATVIEWS OP PROD ----------------------------------------
if($Refresh){
    Need-Prod
    Info "VACUUM (ANALYZE) + matviews verversen op prod (parallelisme uit i.v.m. kleine /dev/shm)..."
    # VACUUM staat bewust VOORAAN en is NIET optioneel:
    #   Een bulk-load/upsert (of een ALTER die de tabel herschrijft) laat de
    #   visibility map leeg achter -> relallvisible = 0 -> Postgres kan geen
    #   index-only scan doen voor count(*) en facet-GROUP BY's en moet elke
    #   keer de volle 1,35 GB heap lezen. Op de Railway-PostGIS (cgroup-cap
    #   1,86 GB, DB 55 GB) blijft die heap niet in page cache: ~23-46 s per
    #   scan, over de statement_timeout van 20 s -> 500 op /stats, /facets en
    #   /pins -> de site toont overal 0 (gebeurd na de load van 24-07-2026).
    #   VACUUM zonder FULL is non-blocking; reads/writes lopen door.
    $sql = @'
SET max_parallel_workers_per_gather = 0;
SET max_parallel_maintenance_workers = 0;

\echo '-- VACUUM (ANALYZE) vth.vergunningkennisgeving (vult de visibility map)'
VACUUM (ANALYZE) vth.vergunningkennisgeving;

SELECT relpages, relallvisible,
       round(100.0*relallvisible/nullif(relpages,0),1) AS pct_visible
FROM pg_class WHERE oid='vth.vergunningkennisgeving'::regclass;

DO $$
BEGIN
  IF to_regclass('vth.dossier_doorlooptijd') IS NULL THEN
    RAISE NOTICE 'matview vth.dossier_doorlooptijd bestaat niet op prod — overgeslagen';
    RETURN;
  END IF;
  BEGIN
    EXECUTE 'REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd';
    RAISE NOTICE 'refreshed CONCURRENTLY';
  EXCEPTION WHEN OTHERS THEN
    -- CONCURRENTLY vereist een unique index + genoeg resources; val terug.
    EXECUTE 'REFRESH MATERIALIZED VIEW vth.dossier_doorlooptijd';
    RAISE NOTICE 'refreshed (non-concurrent fallback)';
  END;
END $$;

-- Header-totalen voor /v1/vergunningen/stats (2026-07-add-vergunning-stats-matview.sql).
DO $$
DECLARE mv text;
BEGIN
  FOREACH mv IN ARRAY ARRAY['vth.vergunning_stats','vth.vergunning_stats_type_besluit'] LOOP
    IF to_regclass(mv) IS NULL THEN
      RAISE NOTICE 'matview % bestaat niet op prod — draai eerst 2026-07-add-vergunning-stats-matview.sql', mv;
      CONTINUE;
    END IF;
    BEGIN
      EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %s', mv);
      RAISE NOTICE '% refreshed CONCURRENTLY', mv;
    EXCEPTION WHEN OTHERS THEN
      EXECUTE format('REFRESH MATERIALIZED VIEW %s', mv);
      RAISE NOTICE '% refreshed (non-concurrent fallback)', mv;
    END;
  END LOOP;
END $$;
'@
    $tmp = Join-Path $WorkDir 'koop_refresh.sql'
    Set-Content -Path $tmp -Value $sql -Encoding utf8
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -f $tmp
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if($LASTEXITCODE -ne 0){ throw "vacuum/matview-refresh faalde (exit $LASTEXITCODE)" }
    Ok "VACUUM gedaan en matviews ververst."
}

# ---- 4. VERIFY ------------------------------------------------------------
if($Verify){
    Need-Prod
    Info "Verificatie op prod..."
    # NB: leest de totalen uit de matview, niet live — een live count(*) is een
    # seq scan van 1,35 GB (zie de VACUUM-toelichting in fase 3). Wijkt
    # stats_refreshed_at af van last_ingest, dan is fase 3 overgeslagen.
    & $psql $ProdUrl -v ON_ERROR_STOP=1 -c @"
SELECT 'vth.vergunningkennisgeving (matview)' AS tabel,
       total::text AS n,
       last_publicatie::text AS max_datum,
       refreshed_at::text AS stats_refreshed_at
FROM vth.vergunning_stats
UNION ALL
SELECT 'vth.dossier_doorlooptijd', count(*)::text, '', ''
FROM vth.dossier_doorlooptijd;
"@
    if($LASTEXITCODE -ne 0){ throw "verify faalde (exit $LASTEXITCODE)" }
    Ok "Klaar. Daarna: Railway TCP-proxy weer UITZETTEN."
}
