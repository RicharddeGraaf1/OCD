-- ============================================================================
-- 2026-08 · werk-index op v2a.tekst_embedding
--
-- Nodig voor GET /v1/viewer/regeling/{expression}/onderwerpen. Dat endpoint
-- zoekt op het FRBR-WERK en niet op de expressie, om twee redenen:
--
--   1. v2a.tekst_embedding draagt de expressie van het moment waarop de
--      vector-laag draaide, en die loopt achter op p2p.regeling. Voor het
--      Arnhemse omgevingsplan staat er `…@2026-03-05` terwijl het register
--      `…@2026-06-26` toont — joinen op expressie geeft daar nul onderwerpen.
--
--   2. Dezelfde kolom bevat twee vormen: voor datzelfde plan 6.420 rijen onder
--      het kale werk én 1.722 onder de expressie. `split_part(…, '/nld@', 1)`
--      normaliseert allebei; een `LIKE …/nld@%` telt stilzwijgend de helft niet
--      mee.
--
-- Zonder deze index is dat predicaat een seq scan over ~1,65 miljoen rijen:
-- gemeten 17,5 s koud. Met index 0,11–0,17 s. Bouwtijd ~3 s.
--
-- Idempotent. Run:
--   psql -h localhost -p 5434 -d dso -f scripts/2026-08-05-add-tekst-embedding-werk-index.sql
-- ============================================================================

CREATE INDEX IF NOT EXISTS tekst_embedding_werk_idx
    ON v2a.tekst_embedding ((split_part(regeling_expression, '/nld@', 1)));

COMMENT ON INDEX v2a.tekst_embedding_werk_idx IS
    'FRBR-werk uit regeling_expression. Voor de onderwerp-as: de vector-laag '
    'loopt achter op p2p.regeling, dus koppelen gebeurt op werk, niet op '
    'expressie.';
