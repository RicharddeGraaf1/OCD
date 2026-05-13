-- ============================================================================
-- 2026-05 · pg_trgm GIN-index op p2p.tekst_element.inhoud_plain
--
-- Nodig om de matview p2p.naammatch_signaal (zie
-- 2026-05-add-naammatch-signaal.sql) binnen redelijke tijd te kunnen
-- builden. Zonder deze index doet Postgres een Cartesisch product van
-- 470K tekst-elementen × 38K objectnamen — uren werk. Met de index
-- kan de matview-CREATE een ILIKE-prefilter gebruiken om per naam
-- snel kandidaat-tekst-elementen te vinden, en alleen daarop de
-- duurdere word-boundary-regex te draaien.
--
-- Achtergrond: zie [[gaps]] G-68 in de vault.
--
-- Idempotent: CREATE EXTENSION IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
-- Eenmalige build-tijd: ~10-30 min op 470K rijen. Disk: ~1-2 GB.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-trgm-index.sql
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_te_inhoud_plain_trgm
    ON p2p.tekst_element
    USING gin (inhoud_plain gin_trgm_ops);

COMMENT ON INDEX p2p.idx_te_inhoud_plain_trgm IS
    'Trigram GIN-index op inhoud_plain voor ILIKE-prefilter in '
    'naammatch_signaal-matview. Maakt per-naam-lookup index-friendly.';
