-- ============================================================================
-- 2026-06 · GIO-referentie-consistentie (drieslag, GIO-tak)
--
-- Matview die per (tekst_element, GIO) detecteert of de tekst de naam van een
-- GIO (`naam_informatieobject`) noemt, en zo ja of er een IntIoRef vanuit dat
-- tekst_element naar die GIO bestaat. Dit is de GIO-spiegel van
-- `tekst_object_consistentie` (die OW-objecten via annotatie/naam-match dekt).
--
-- Klassen:
--   • consistent_gio_referentie            — naam-match én IntIoRef aanwezig
--   • vermoedelijk_vergeten_gio_referentie — naam-match maar GEEN IntIoRef
--                                            (= odkwaliteit-richtlijn H44)
--
-- Heuristiek identiek aan p2p.naammatch_signaal: exact word-boundary match,
-- case-insensitief, minimumlengte 5, INTRA-regeling. Vereist de pg_trgm
-- GIN-index op tekst_element.inhoud_plain + idx_gio_naam_io_trgm.
--
-- Bewust additief (eigen matview) i.p.v. een tak in tekst_object_consistentie,
-- om CASCADE/refresh-risico op de grote productie-view te vermijden. odkwaliteit
-- leest deze matview náást tekst_object_consistentie_mv.
--
-- Idempotent: DROP IF EXISTS + CREATE.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-gio-referentie-consistentie.sql
--       Vervolgens: REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.gio_referentie_consistentie_mv;
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS p2p.gio_referentie_consistentie_mv CASCADE;

CREATE MATERIALIZED VIEW p2p.gio_referentie_consistentie_mv AS
SELECT
    te.id                 AS tekst_element_id,
    g.frbr_expression     AS gio_frbr,
    te.regeling_expression,
    g.naam_informatieobject AS gematchte_naam,
    CASE WHEN EXISTS (
        SELECT 1 FROM p2p.tekst_inline_referentie tir
        WHERE tir.tekst_element_id = te.id
          AND tir.soort = 'IntIoRef'
          AND tir.target_gio_expression = g.frbr_expression
    ) THEN 'consistent_gio_referentie'
      ELSE 'vermoedelijk_vergeten_gio_referentie'
    END AS consistentie_klasse
FROM p2p.tekst_element te
JOIN p2p.geo_informatieobject g
  ON g.regeling_expression = te.regeling_expression
 AND g.naam_informatieobject IS NOT NULL
 AND length(g.naam_informatieobject) >= 5
 AND te.inhoud_plain IS NOT NULL
 -- ILIKE prefilter (pg_trgm GIN-index) — zonder dit een Cartesisch product
 AND te.inhoud_plain ILIKE '%' || g.naam_informatieobject || '%'
 -- exact word-boundary match
 AND te.inhoud_plain ~* (
     '\m' ||
     regexp_replace(g.naam_informatieobject, '([\.\^\$\*\+\?\(\)\[\]\{\}\\\|])', '\\\1', 'g') ||
     '\M'
 );

CREATE UNIQUE INDEX gio_ref_consistentie_pk
    ON p2p.gio_referentie_consistentie_mv (tekst_element_id, gio_frbr);
CREATE INDEX gio_ref_consistentie_regeling
    ON p2p.gio_referentie_consistentie_mv (regeling_expression);
CREATE INDEX gio_ref_consistentie_klasse
    ON p2p.gio_referentie_consistentie_mv (consistentie_klasse);

COMMENT ON MATERIALIZED VIEW p2p.gio_referentie_consistentie_mv IS
    'GIO-tak van de drieslag tekst↔object: per (tekst_element, GIO) of de tekst de '
    'naam_informatieobject noemt en of er een IntIoRef naartoe is. Klasse '
    'vermoedelijk_vergeten_gio_referentie = naam-match zonder IntIoRef (H44). '
    'Heuristiek — word-boundary, intra-regeling, minlengte 5.';
