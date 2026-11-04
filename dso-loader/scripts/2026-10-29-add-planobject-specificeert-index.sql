-- ============================================================================
-- 2026-10-29 · index op wro.planobject.specificeert_id
--
-- specificeert_id is een FK op wro.planobject zelf. Bij elke verwijderde rij
-- zoekt Postgres of een ander planobject ernaar verwijst -- en zonder index op
-- die kolom is dat een volledige scan van 14-16 GB, PER RIJ. De loader merkte
-- dat nooit (die doet alleen upserts), maar wro_planobject_naar_productie.py
-- verwijdert de planobjecten van een plan voor hij ze opnieuw laadt: een plan
-- met 68 objecten stond na 5 minuten nog op DataFileRead.
--
-- Partieel, want geen enkel planobject vult de kolom (0 van 6,37 mln op
-- 2026-10-29). De FK-controle (`specificeert_id = $1`) impliceert IS NOT NULL,
-- dus de planner gebruikt hem wel.
--
-- CONCURRENTLY: geen schrijfslot op de tabel; mag niet in een transactie, dus
-- los draaien (psql -f, of autocommit).
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_planobject_specificeert
    ON wro.planobject (specificeert_id) WHERE specificeert_id IS NOT NULL;
