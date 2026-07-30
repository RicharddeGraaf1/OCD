-- ============================================================================
-- 2026-07 · B-tree index op p2p.activiteit_locatieaanduiding.locatie_id
--
-- Elke punt-query in de viewer ("welke regelingen/objecten gelden hier?")
-- start bij locatie_subdiv (GIST-index, ~65 treffers) en joint dan naar
-- activiteit_locatieaanduiding op locatie_id. Op die kolom stond geen index:
--   - idx_ala_activiteit  = (activiteit_id)
--   - idx_ala_regel       = (juridische_regel_id)
--   - uq_ala_natural      = (juridische_regel_id, activiteit_id, locatie_id,
--                            kwalificatie)  -> locatie_id staat op positie 3
--                            en is dus niet als leidende sleutel bruikbaar
--
-- Gevolg: Postgres deed een seq scan over alle 383.793 rijen (13.620 pages)
-- voor élke locatie-lookup. Warm kost dat ~0,35s extra, koud ~13s aan
-- disk-reads -- dat was de dominante kost van GET /v1/viewer/regelingen
-- (14,0s koud / 0,80s warm gemeten op punt 136000/455000, Utrecht).
--
-- Na deze index + de query-rewrite in ocd-api/main.py (viewer_regelingen):
--   0,58s -> 0,165s warm, en 12.190 disk-page-reads -> 0.
--
-- Raakt naast /v1/viewer/regelingen ook /v1/viewer/objecten, /v1/viewer/ala
-- en /v1/viewer/regelmix -- die starten allemaal op dezelfde join.
--
-- Idempotent. Build-tijd ~0,5s, disk ~3,5 MB.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-07-add-ala-locatie-index.sql
-- Prod: idem tegen de Railway-DB (CONCURRENTLY is niet nodig; de build is
--       sub-seconde, maar bij een live-prod kun je 'm ook CONCURRENTLY doen).
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_ala_locatie
    ON p2p.activiteit_locatieaanduiding (locatie_id);

ANALYZE p2p.activiteit_locatieaanduiding;
