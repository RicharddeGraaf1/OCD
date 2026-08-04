-- ============================================================================
-- 2026-08 · index op v2a.tekst_embedding (regeling_expression).
--
-- Aanleiding: elke query die "zoek binnen de chunks van DEZE regeling" doet, had
-- geen bruikbare index. De planner koos dan een parallelle seq scan over de volle
-- 1,65 mln rijen en filterde pas daarna op de regeling. Gemeten op de
-- kandidaat-query van instructieregels.nl (ILIKE binnen één omgevingsplan):
--
--     zonder index : 628.199 buffers (~5 GB gelezen), 2-5 s per aanroep
--     met index    :   2.748 buffers                          (factor 230)
--
-- Doorlooptijd van die pijplijn ging van 16,1 naar 2,4 s per instructieregel
-- (~10 u -> ~1,7 u). Raakt ook de per-document vector-screening van de
-- doeldocument-pijplijn en elke andere per-regeling-scoped chunk-query.
--
-- CONCURRENTLY: geen schrijf-lock op de tabel, mag tijdens gebruik.
-- Kost ~14 MB. Idempotent.
--
-- Let op: CREATE INDEX CONCURRENTLY kan niet binnen een transactieblok — dit
-- script dus niet in een BEGIN/COMMIT wikkelen of via een migratierunner draaien
-- die dat impliciet doet.
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tekst_embedding_regeling_expr
    ON v2a.tekst_embedding (regeling_expression);
