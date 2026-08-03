-- 2026-08-03 — Zoeken en sorteren op /v1/vergunningen werkend maken.
--
-- Onderdeel 1: index voor sorteren op datum_ontvangst.
-- Onderdeel 2: GIN-tsvector voor het q-filter (bestaat al op prod, hier voor dev).
--
-- Aanleiding: bij het uitzoeken van "de site is de eerste keer traag" bleken er
-- naast de bbox-count (opgelost in de API met COUNT_CAP) nog twee query-paden
-- structureel over de statement_timeout van 20 s te gaan. Gemeten op prod
-- 2026-08-03 met EXPLAIN (ANALYZE, BUFFERS) via `railway ssh`.
--
-- IDEMPOTENT: veilig herhaalbaar (CREATE ... IF NOT EXISTS).
-- Draaien:  psql "$PROD_URL" -v ON_ERROR_STOP=1 -f 2026-08-vergunningen-zoek-en-sortering.sql
--
-- LET OP 1 — CONCURRENTLY: dit script bouwt indexen CONCURRENTLY, zodat de site
--   blijft draaien. Dat kan NIET in een transactieblok; draai het dus met psql
--   zonder BEGIN eromheen (default). Duurt langer dan een gewone build.
-- LET OP 2 — parallelisme: de /dev/shm van de Railway-PostGIS is 64 MB, waar
--   parallelle maintenance-workers op stuklopen. Daarom staat het hieronder uit;
--   draai je onderdelen los, gebruik dan
--   PGOPTIONS="-c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0".

SET max_parallel_maintenance_workers = 0;
SET max_parallel_workers_per_gather = 0;

-- ── ONDERDEEL 1: sorteren op datum ontvangst ──────────────────────────
--
-- PROBLEEM
--   sort=ontvangst geeft `ORDER BY datum_ontvangst DESC NULLS LAST, koop_id DESC`
--   over de volle tabel. De bestaande partiële index idx_vk_datum_ontvangst is
--   oplopend én dekt geen NULLs, dus bruikbaar is hij niet: Postgres sorteert
--   alle 883k rijen. Gemeten:
--     sort=ontvangst zonder bbox   20,17 s  -> 500
--     sort=ontvangst met bbox      18,42 s  -> 500
--   Ter vergelijking: sort=bg zonder bbox 0,01 s, sort=datum 0,04 s.
--
--   Maar 3,8% van de records heeft een datum_ontvangst; de overige ~96% staat
--   per definitie achteraan. Toch nemen we ze mee in de index (dus geen partiële
--   index) omdat de lijst ze wél toont — de sortering betekent "op ontvangst,
--   rest onderaan", niet "alleen records mét ontvangstdatum".
--
-- Kolomvolgorde en NULL-plaatsing zijn LETTERLIJK gelijk aan _SORT_SQL['ontvangst']
-- in ocd-api/vergunningen.py. Wijkt er één van af, dan kan de planner de index
-- niet gebruiken voor de sort en zijn we terug bij af.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vk_ontvangst_sort
    ON vth.vergunningkennisgeving (datum_ontvangst DESC NULLS LAST, koop_id DESC);

-- ── ONDERDEEL 2: full-text index voor het q-filter ────────────────────
--
-- PROBLEEM
--   Het q-filter deed vijf maal ILIKE '%term%' (titel, beschrijving,
--   inhoud_tekst, straatnaam, woonplaats). Een leading wildcard kan nooit een
--   index gebruiken, dus élke zoekopdracht was een seq scan over 5,8 GB en gaf
--   500 na 20 s — op /vergunningen, /pins én /facets tegelijk.
--
--   Op productie stond deze GIN-index al (305 MB, aangelegd bij een eerdere
--   ronde) maar werd hij door geen enkele query gebruikt. De API doet nu
--   `@@ to_tsquery('dutch', ...)` met exact deze expressie. Gemeten na de
--   omzetting: 'zonnepark' 0,41 s, 'Kalverstraat' 0,21 s.
--
--   De expressie moet LETTERLIJK gelijk zijn aan _TSV_EXPR in
--   ocd-api/vergunningen.py, anders valt de planner terug op to_tsvector() per
--   rij. Controle: SELECT indexdef FROM pg_indexes WHERE indexname='idx_vk_tsv';
--
-- Op een dev-DB duurt deze build een paar minuten; op prod bestaat hij al en
-- is dit een no-op.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_vk_tsv
    ON vth.vergunningkennisgeving
    USING gin (to_tsvector('dutch',
        coalesce(titel,'') || ' ' || coalesce(beschrijving,'') || ' ' ||
        coalesce(inhoud_tekst,'') || ' ' || coalesce(straatnaam,'') || ' ' ||
        coalesce(woonplaats,'')));

ANALYZE vth.vergunningkennisgeving;

-- ── 3. Controle ────────────────────────────────────────────────────────
-- Beide indexen moeten bestaan én valid zijn (een afgebroken CONCURRENTLY-build
-- laat een INVALID index achter die niet gebruikt wordt — die moet je droppen
-- en opnieuw bouwen).
SELECT c.relname AS index, i.indisvalid AS valid,
       pg_size_pretty(pg_relation_size(c.oid)) AS grootte
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname IN ('idx_vk_ontvangst_sort', 'idx_vk_tsv');
