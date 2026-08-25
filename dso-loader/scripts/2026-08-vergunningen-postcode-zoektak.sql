-- 2026-08-22 — Zoeken op postcode werkend maken op /v1/vergunningen.
--
-- AANLEIDING
--   De zoekbalk van omgevingsvergunningenregister.nl belooft in zijn placeholder
--   "Zoek op postcode, adres, zaaknummer of publicatie-id…", maar de kolom
--   `postcode` zit niet in _TSV_EXPR en had geen eigen tak in _q_filter. Een
--   postcode ging dus door de generieke full-text-tak, en die maakt hem stuk:
--   _tsquery_arg splitst op niet-alfanumeriek, dus "1097 PR" wordt
--   `1097:* & PR:*` — en `PR:*` matcht "Professor", "procedure", "provincie".
--
--   Gemeten 2026-08-22 op de lokale DB (899.540 records):
--     invoer      kolom-match   wat de API deed
--     1097PR                0   2   (alleen toevallige body-hits)
--     1097 PR               0   635 waarvan 173 werkelijk in 1097xx
--     3454CR                6   4
--   Eerste treffer op "1097 PR" was "Kennisgeving WET BODEMBESCHERMING".
--
-- WAT DE API NU DOET (vergunningen.py, _POSTCODE_PATROON)
--   Invoer die matcht op ^(\d{4})\s*([A-Za-z]{2})$ wordt genormaliseerd naar de
--   aaneengeschreven hoofdletter-vorm en krijgt een eigen clause met twee armen:
--     (postcode = '1097PR' OR <_TSV_EXPR> @@ to_tsquery('dutch', '1097pr'))
--   Arm 1 dekt de kolom (37,96% van de records gevuld). Arm 2 dekt de body-tekst,
--   waar de dutch-parser van "…99 1097RA Amsterdam" één token '1097ra' maakt —
--   geverifieerd met to_tsvector(), zie de commit-boodschap.
--
-- IDEMPOTENT: veilig herhaalbaar (CREATE ... IF NOT EXISTS).
-- Draaien:  psql "$PROD_URL" -v ON_ERROR_STOP=1 -f 2026-08-vergunningen-postcode-zoektak.sql
--
-- LET OP 1 — CONCURRENTLY kan NIET in een transactieblok; draai met psql zonder
--   BEGIN eromheen (default).
-- LET OP 2 — parallelisme: de /dev/shm van de Railway-PostGIS is 64 MB, waar
--   parallelle maintenance-workers op stuklopen. Daarom staat het hieronder uit.

SET max_parallel_maintenance_workers = 0;
SET max_parallel_workers_per_gather = 0;

-- ── Index voor arm 1 ───────────────────────────────────────────────────
--
-- Partieel op NOT NULL: 341.467 van de 899.540 records heeft een postcode, dus
-- de index is ~62% kleiner dan een volledige. De overige rijen kunnen per
-- definitie nooit matchen op `postcode = 'x'`, dus de planner mist niets.
--
-- Zonder deze index is de equality een seq scan over 5,8 GB (~18 s gemeten,
-- vlak onder de statement_timeout van 20 s) — en dat is precies het pad dat
-- elke postcode-zoekopdracht zou nemen.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vk_postcode
    ON vth.vergunningkennisgeving (postcode)
    WHERE postcode IS NOT NULL;

ANALYZE vth.vergunningkennisgeving;

-- ── Controle ───────────────────────────────────────────────────────────
-- De index moet bestaan én valid zijn (een afgebroken CONCURRENTLY-build laat
-- een INVALID index achter die niet gebruikt wordt — droppen en opnieuw bouwen).
SELECT c.relname AS index, i.indisvalid AS valid,
       pg_size_pretty(pg_relation_size(c.oid)) AS grootte
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname = 'idx_vk_postcode';

-- Verwacht plan: BitmapOr over idx_vk_postcode en idx_vk_tsv, geen seq scan.
EXPLAIN (FORMAT TEXT)
SELECT 1 FROM vth.vergunningkennisgeving
WHERE (postcode = '1097PR'
       OR to_tsvector('dutch',
              coalesce(titel,'') || ' ' || coalesce(beschrijving,'') || ' ' ||
              coalesce(inhoud_tekst,'') || ' ' || coalesce(straatnaam,'') || ' ' ||
              coalesce(woonplaats,'')) @@ to_tsquery('dutch', '1097pr'));
