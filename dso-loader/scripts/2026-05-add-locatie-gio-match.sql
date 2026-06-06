-- ============================================================================
-- 2026-05 · locatie_gio_match — direct GIO ↔ Locatie via set-equality
--
-- Voor queries als "welk GIO hoort bij deze Locatie?" of "welke Locaties
-- wijst dit GIO aan?" — niet nodig voor de drieslag-keten (die werkt op
-- basisgeo:id-niveau direct), maar wel nuttig voor:
--   - OCD-viewer: van Locatie naar haar geometrie (via GIO-FRBR)
--   - Data-kwaliteit: Gebiedengroepen zonder corresponderende GIO
--   - Backward-tracing: alle Locaties die een GIO aanwijst
--
-- Match-type: EXACT (set-equality op basisgeo:ids). Uitbreidbaar met
-- subset / overlap types als blijkt dat die nodig zijn.
--
-- Idempotent: CREATE OR REPLACE VIEW.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-locatie-gio-match.sql
-- ============================================================================

CREATE OR REPLACE VIEW p2p.locatie_gio_match AS
WITH
  loc_sets AS (
    SELECT locatie_id,
           array_agg(basisgeo_id ORDER BY basisgeo_id) AS bg_set,
           COUNT(*) AS bg_count
    FROM p2p.locatie_basisgeo
    GROUP BY locatie_id
  ),
  gio_sets AS (
    SELECT gio_frbr,
           array_agg(basisgeo_id ORDER BY basisgeo_id) AS bg_set,
           COUNT(*) AS bg_count
    FROM p2p.gio_basisgeo
    GROUP BY gio_frbr
  )
SELECT
    l.locatie_id,
    g.gio_frbr,
    l.bg_count,
    'exact'::TEXT AS match_type
FROM loc_sets l
JOIN gio_sets g ON l.bg_set = g.bg_set;

COMMENT ON VIEW p2p.locatie_gio_match IS
    'EXACT-match tussen Locaties en GIOs via set-equality op basisgeo:ids. '
    'Per (locatie_id, gio_frbr) één rij als beide kanten exact dezelfde '
    'set basisgeo:ids hebben. Niet nodig voor drieslag-keten; wel voor '
    'OCD-viewer-queries en data-kwaliteits-analyses. Uitbreidbaar met '
    'subset/overlap match_types indien gewenst.';
