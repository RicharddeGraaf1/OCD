-- 2026-07-28 — Hot path van /v1/vergunningen weer index-driven maken.
--
-- Onderdeel 1: twee ontbrekende indexen (facet organisatietype + pins-geo).
-- Onderdeel 2: /stats van seq scan naar matview.
--
-- Aanleiding: na de load van 24-07-2026 stond omgevingsvergunningenregister.nl
-- op 0 — /stats, /facets en /pins liepen alle drie in de statement_timeout van
-- 20 s (db.py). Een VACUUM (ANALYZE) op vth.vergunningkennisgeving loste het
-- grootste deel op (visibility map was leeg -> geen index-only scans; count(*)
-- ging van 23-46 s naar 0,06 s), maar drie queries hebben geen index om op te
-- landen. Dit script dicht die.
--
-- IDEMPOTENT: veilig herhaalbaar (CREATE ... IF NOT EXISTS / DROP ... IF EXISTS).
-- Draaien:  psql "$PROD_URL" -v ON_ERROR_STOP=1 -f 2026-07-vergunningen-hot-path.sql
--
-- LET OP: CREATE INDEX (zonder CONCURRENTLY) neemt een ShareLock — reads lopen
-- door, writes wachten. Draai dit dus niet tijdens een -Push van de loader.
--
-- ── ONDERDEEL 1: ontbrekende indexen ──────────────────────────────────
--
-- Gemeten op prod NA de VACUUM, non-parallel (zoals de API-pool):
--   GROUP BY organisatietype                    35,6 s  -> geen index
--   count(*) WHERE geometrie_wgs_pt IS NOT NULL 34,9 s  -> GiST kan geen
--                                                          IS NOT NULL
--   GROUP BY activiteit_code / subject_taxonomie / publicatieblad  ~0,09 s
--   GROUP BY bg_naam (top 100)                                     0,5 s

-- Parallelisme uit: de dynamic-shared-memory-segmenten van parallelle workers
-- lopen op de kleine /dev/shm (64 MB) stuk met
-- "could not resize shared memory segment ... No space left on device".
-- Geldt voor de index-builds, de matview-scans én een losse VACUUM (draai die
-- met PGOPTIONS="-c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0").
SET max_parallel_maintenance_workers = 0;
SET max_parallel_workers_per_gather = 0;

-- Facet organisatietype: /facets aggregeert hierop bij élke filterwijziging.
CREATE INDEX IF NOT EXISTS idx_vk_organisatietype
    ON vth.vergunningkennisgeving (organisatietype);

-- Pins: /pins forceert `geometrie_wgs_pt IS NOT NULL` en sorteert op
-- datum_publicatie DESC, koop_id DESC met een LIMIT (cap 10k). Deze partiële
-- index dient beide: de count(*) wordt index-only over alléén de rijen mét
-- geometrie, en de ORDER BY ... LIMIT kan hem aflopen zonder sort.
CREATE INDEX IF NOT EXISTS idx_vk_geom_notnull_datum
    ON vth.vergunningkennisgeving (datum_publicatie DESC, koop_id DESC)
    WHERE geometrie_wgs_pt IS NOT NULL;

-- Pins + facetfilter. `geometrie_wgs_pt IS NOT NULL` filtert vrijwel niets weg
-- (834.180 van 875.950 rijen = 95,2%), dus zodra er óók een niet-selectief
-- facetfilter bij komt, kan Postgres nergens op landen en scant hij de hele
-- heap. Gemeten count(*) met geo + één filter, vóór deze index:
--   type_besluit=aanvraag   60,6 s      bg_naam=Amsterdam        12,8 s
--   activiteit_code=bouwen  62,2 s      datum_publicatie >= ...   0,06 s
--   organisatietype=gemeente 62,3 s     (die twee zitten al goed)
--
-- Bewust één DEKKENDE index i.p.v. losse partiële indexen per kolom: bij losse
-- indexen wordt een combinatie (tb=aanvraag EN ac=bouwen) een bitmap-AND, en
-- een bitmap heap scan kan nooit index-only zijn -> dan zijn we terug bij
-- honderdduizenden heap-fetches. Met alle facetkolommen in één index blijft
-- élke deelverzameling index-only: op type_besluit als range-scan, op de
-- overige kolommen desnoods als volledige scan van deze index (~115 MB, past
-- in de page cache) in plaats van 1,35 GB heap.
--
-- Kolomvolgorde: type_besluit vooraan omdat de viewer die checkbox-groep
-- altijd meestuurt; subject_taxonomie achteraan omdat hij met 47 bytes/rij
-- veruit het breedst is. bg_naam (11 bytes) en subject_taxonomie zitten erin
-- omdat ze zónder deze index 15,4 s resp. 34,6 s kostten op hun grootste
-- waarde — allebei binnen klikafstand in de viewer.
CREATE INDEX IF NOT EXISTS idx_vk_geom_notnull_facet
    ON vth.vergunningkennisgeving
       (type_besluit, activiteit_code, organisatietype, publicatieblad,
        bg_naam, subject_taxonomie, datum_publicatie DESC, koop_id DESC)
    WHERE geometrie_wgs_pt IS NOT NULL;

ANALYZE vth.vergunningkennisgeving;

-- ── ONDERDEEL 2: stats-matviews ───────────────────────────────────────
--
-- PROBLEEM
--   `/v1/vergunningen/stats` deed twee volledige scans over
--   vth.vergunningkennisgeving (876k rijen, heap 1,35 GB):
--     1) count(*) + max(datum_publicatie_ts) + max(ingest_at)
--        + count(*) FILTER (WHERE inhoud_tekst IS NOT NULL)
--     2) GROUP BY type_besluit
--   Geen van die aggregaties is index-only te maken: datum_publicatie_ts,
--   ingest_at en inhoud_tekst hebben geen index. Op de Railway-PostGIS
--   (cgroup-cap 1,86 GB, database 55 GB) blijft de heap niet in page cache;
--   een scan kost 23-46 s en tikt dus tegen de statement_timeout van 20 s
--   (db.py) -> 500 -> de site toont overal 0.
--
-- OPLOSSING
--   Twee minuscule matviews die één keer per refresh-ronde worden ververst
--   (zie refresh-koop-to-prod.ps1 -Refresh). Beide met unique index zodat
--   REFRESH ... CONCURRENTLY kan (geen AccessExclusiveLock tijdens de ~30 s
--   scan, dus /stats blijft ondertussen antwoorden).
--
--   `refreshed_at` legt vast wannéér de aggregaten zijn berekend, zodat het
--   verschil met last_ingest zichtbaar is als de refresh is overgeslagen.
--
-- ── 2a. Totalen (exact één rij) ────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS vth.vergunning_stats;

CREATE MATERIALIZED VIEW vth.vergunning_stats AS
SELECT 1                                                        AS id,
       count(*)                                                 AS total,
       max(datum_publicatie_ts)                                 AS last_publicatie,
       max(ingest_at)                                           AS last_ingest,
       count(*) FILTER (WHERE inhoud_tekst IS NOT NULL)          AS enriched,
       now()                                                    AS refreshed_at
FROM vth.vergunningkennisgeving;

CREATE UNIQUE INDEX vergunning_stats_pk ON vth.vergunning_stats (id);

COMMENT ON MATERIALIZED VIEW vth.vergunning_stats IS
  'Header-totalen voor /v1/vergunningen/stats. Verversen na elke koop-ingest '
  '(refresh-koop-to-prod.ps1 -Refresh). Bestaat omdat de live aggregatie een '
  'seq scan van 1,35 GB is en op prod de 20s statement_timeout raakt.';

-- ── 2b. Verdeling per type_besluit ─────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS vth.vergunning_stats_type_besluit;

CREATE MATERIALIZED VIEW vth.vergunning_stats_type_besluit AS
SELECT type_besluit AS value,
       count(*)     AS count
FROM vth.vergunningkennisgeving
WHERE type_besluit IS NOT NULL
GROUP BY 1;

CREATE UNIQUE INDEX vergunning_stats_type_besluit_pk
    ON vth.vergunning_stats_type_besluit (value);

COMMENT ON MATERIALIZED VIEW vth.vergunning_stats_type_besluit IS
  'Aantal kennisgevingen per type_besluit, ongefilterd. Zie vth.vergunning_stats.';

-- ── 3. Controle ────────────────────────────────────────────────────────
SELECT indexname FROM pg_indexes
WHERE schemaname='vth' AND tablename='vergunningkennisgeving'
  AND indexname IN ('idx_vk_organisatietype','idx_vk_geom_notnull_datum',
                    'idx_vk_geom_notnull_facet');

SELECT total, enriched, last_publicatie, last_ingest, refreshed_at
FROM vth.vergunning_stats;

SELECT value, count FROM vth.vergunning_stats_type_besluit ORDER BY count DESC;
