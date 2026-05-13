-- ============================================================================
-- 2026-05 · Hoofdlijn-soort-mapping
--
-- Voegt core.hoofdlijn_soort_mapping toe en seedt 'm met de huidige distinct
-- soort-waarden uit p2p.hoofdlijn. Initial canonical = LOWER(TRIM(raw)) als
-- best-effort eerste pass — alle entries reviewed=FALSE zodat een mens ze
-- nog kan opschonen (Ambitie/ambitie/Ambities consolideren, ad-hoc beleids-
-- teksten naar 'Onderwerp' mappen, '-' naar 'Overig', etc.).
--
-- Idempotent: ON CONFLICT DO NOTHING bij re-run; bestaande gereviewde
-- mappings blijven bewaard.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-hoofdlijn-mapping.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS core.hoofdlijn_soort_mapping (
    raw_value   TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL,
    reviewed    BOOLEAN NOT NULL DEFAULT FALSE,
    notitie     TEXT NULL
);

CREATE INDEX IF NOT EXISTS hoofdlijn_soort_mapping_canonical_idx
    ON core.hoofdlijn_soort_mapping (canonical);

-- Initial auto-fill: lowercase + trim. '-'-placeholder krijgt 'Overig'.
-- Onbekende ad-hoc waarden krijgen hun eigen lowercase als canonical;
-- review-stap kan ze later samenvoegen tot een kleinere taxonomie.
INSERT INTO core.hoofdlijn_soort_mapping (raw_value, canonical, reviewed)
SELECT DISTINCT
    soort                                AS raw_value,
    CASE
        WHEN TRIM(soort) IN ('-', '')   THEN 'Overig'
        ELSE LOWER(TRIM(soort))
    END                                  AS canonical,
    FALSE                                AS reviewed
FROM p2p.hoofdlijn
WHERE soort IS NOT NULL
ON CONFLICT (raw_value) DO NOTHING;

-- Sanity-check: hoeveel raw-waarden zijn er, en hoeveel distinct canonical
-- na de auto-pass? Als die laatste nog steeds groot is (>15) dan is
-- handmatige consolidatie nodig.
\echo '── Mapping-status ──'
SELECT
    COUNT(*)                          AS aantal_raw,
    COUNT(DISTINCT canonical)         AS aantal_canonical,
    COUNT(*) FILTER (WHERE reviewed)  AS reviewed_count
FROM core.hoofdlijn_soort_mapping;

\echo '── Top-10 canonical-buckets na auto-fill ──'
SELECT canonical, COUNT(*) AS raw_aantal
FROM core.hoofdlijn_soort_mapping
GROUP BY canonical
ORDER BY raw_aantal DESC, canonical
LIMIT 10;
