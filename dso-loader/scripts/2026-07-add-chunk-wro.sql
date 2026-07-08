-- ============================================================================
-- 2026-07 · Fase 6a — wro-chunks in de vectorindex.
--
-- wro heeft GEEN wId/annotatie-pad. Werkingsgebied van een tekst-chunk =
--   COARSE: chunk -> instrument_idn -> wro.ruimtelijk_instrument.geometrie
--           (plangebied; 100% dekking). Opgeslagen als regeling_expression op
--           v2a.tekst_embedding (scope-sleutel), geen aparte tabel nodig.
--   FIJN:   chunk (tekst_object.nummer) -> planobject.artikelnummer -> geometrie
--           (bestemmingsvlak). Slechts ~0,6% matcht (nummer-formaten alignen niet)
--           -> apart tabelletje, best-effort.
--
-- wro-chunks in v2a.tekst_embedding: source_type='wro', source_ref=identificatie,
--   regeling_expression=instrument_idn, bron_soort=object_type (Artikel/Hoofdstuk/...).
--   tekst_element_id/wid = NULL (geen STOP-structuur).
-- ============================================================================

CREATE TABLE IF NOT EXISTS v2a.chunk_wro_object (
    chunk_id       BIGINT NOT NULL REFERENCES v2a.tekst_embedding(id) ON DELETE CASCADE,
    planobject_id  TEXT NOT NULL,     -- wro.planobject.identificatie (heeft geometrie)
    instrument_idn TEXT NOT NULL      -- wro.ruimtelijk_instrument.idn
);
CREATE INDEX IF NOT EXISTS chunk_wro_object_chunk_idx ON v2a.chunk_wro_object (chunk_id);
CREATE INDEX IF NOT EXISTS chunk_wro_object_po_idx    ON v2a.chunk_wro_object (planobject_id);
