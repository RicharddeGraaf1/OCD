-- ============================================================================
-- 2026-05 · tekst_object_consistentie_mv — materialized snapshot voor snelle reads
--
-- De view `p2p.tekst_object_consistentie` (v4) doet ~12s over de hele
-- dataset. Per-regeling-filter (`WHERE te.regeling_expression = ...`)
-- duurt langer (~50s) omdat de filter pas na UNION/EXISTS toegepast
-- wordt. Voor de OCD-viewer is per-regeling-bevraging het normale
-- patroon — daarom een matview-snapshot met regeling_expression als
-- expliciete kolom + index daarop.
--
-- Refresh-strategie:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.tekst_object_consistentie_mv;
-- Vereist UNIQUE INDEX (zie onder). Volgorde bij volledige rebuild:
--   1. naammatch_signaal (~35 min)
--   2. naammatch_signaal_intra (~8 sec)
--   3. tekst_object_consistentie_mv (~10-30 sec)
--
-- Idempotent: DROP IF EXISTS + CREATE.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-object-consistentie-mv.sql
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS p2p.tekst_object_consistentie_mv CASCADE;

CREATE MATERIALIZED VIEW p2p.tekst_object_consistentie_mv AS
SELECT
    toc.tekst_element_id,
    toc.object_type,
    toc.object_id,
    toc.has_annotatie,
    toc.has_intioref,
    toc.has_naammatch,
    toc.doelen_match,
    toc.consistentie_klasse,
    te.regeling_expression
FROM p2p.tekst_object_consistentie toc
JOIN p2p.tekst_element te ON te.id = toc.tekst_element_id;

-- UNIQUE INDEX vereist voor REFRESH CONCURRENTLY
CREATE UNIQUE INDEX tekst_object_consistentie_mv_pk
    ON p2p.tekst_object_consistentie_mv (tekst_element_id, object_type, object_id);

-- Snelle filter per regeling (primaire access pattern voor OCD-viewer)
CREATE INDEX tekst_object_consistentie_mv_regeling
    ON p2p.tekst_object_consistentie_mv (regeling_expression);

-- Snelle filter op klasse (bv. "alle vermoedelijk vergeten in regeling X")
CREATE INDEX tekst_object_consistentie_mv_regeling_klasse
    ON p2p.tekst_object_consistentie_mv (regeling_expression, consistentie_klasse);

-- Snelle filter op object_type
CREATE INDEX tekst_object_consistentie_mv_object_type
    ON p2p.tekst_object_consistentie_mv (object_type);

COMMENT ON MATERIALIZED VIEW p2p.tekst_object_consistentie_mv IS
    'Materialized snapshot van p2p.tekst_object_consistentie + regeling_expression. '
    'Voor snelle per-regeling-bevraging in de OCD-viewer (~ms ipv ~s). '
    'Refresh na elke loader-run; vereist dat naammatch_signaal en '
    'naammatch_signaal_intra eerst zijn gerefresht.';
