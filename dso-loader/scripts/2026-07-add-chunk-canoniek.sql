-- ============================================================================
-- 2026-07 · Fase 3 — canonieke v2a.chunk-laag (dicht gaps G-88).
--
-- NIET-destructief: we hernoemen v2a.tekst_embedding NIET (dat zou /v1/semantisch
-- + chunk_annotatie/chunk_categorie-FK's breken). In plaats daarvan:
--   1. voeg de canonieke kolommen toe: wid, source_type, source_id, source_ref;
--   2. backfill uit de bestaande data (source_type=bron_soort, source_id=tekst_element_id,
--      wid uit tekst_element);
--   3. expose een canonieke VIEW v2a.chunk met de nette kolomnamen.
--
-- Polymorfe source-conventie (voor uitbreiding voorbij tekst_element):
--   source_type = 'Lid'|'Divisietekst'|'Begrip'|'Artikel'  -> source_id = tekst_element.id (bigint)
--   source_type = 'objectnaam-<type>'                       -> source_ref = object-identificatie (text)
--
-- Idempotent (ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE VIEW).
-- ============================================================================

ALTER TABLE v2a.tekst_embedding ADD COLUMN IF NOT EXISTS wid         TEXT;
ALTER TABLE v2a.tekst_embedding ADD COLUMN IF NOT EXISTS source_type TEXT;
ALTER TABLE v2a.tekst_embedding ADD COLUMN IF NOT EXISTS source_id   BIGINT;
ALTER TABLE v2a.tekst_embedding ADD COLUMN IF NOT EXISTS source_ref  TEXT;

-- backfill vanuit bestaande tekst_element-chunks (alleen waar nog niet gezet)
UPDATE v2a.tekst_embedding v
SET source_type = COALESCE(v.source_type, v.bron_soort),
    source_id   = COALESCE(v.source_id, v.tekst_element_id),
    wid         = COALESCE(v.wid, te.wid)
FROM p2p.tekst_element te
WHERE te.id = v.tekst_element_id
  AND (v.source_type IS NULL OR v.source_id IS NULL OR v.wid IS NULL);

CREATE INDEX IF NOT EXISTS tekst_embedding_source_idx ON v2a.tekst_embedding (source_type, source_id);
CREATE INDEX IF NOT EXISTS tekst_embedding_wid_idx    ON v2a.tekst_embedding (wid);

-- canonieke view met nette namen (chunk_id = de surrogaat-PK)
CREATE OR REPLACE VIEW v2a.chunk AS
SELECT id AS chunk_id, source_type, source_id, source_ref, wid,
       regeling_expression, bron_soort, kop_pad, inhoud_plain, embedding, fts
FROM v2a.tekst_embedding;
