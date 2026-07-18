-- ============================================================================
-- 2026-07 · core.gemeentegrens_historisch
--
-- Grenzen van opgeheven gemeenten (uit PDOK CBS Gebiedsindelingen per jaar),
-- als ambtsgebied-bron voor oude IMRO2006-plannen waarvan de bronhouder niet
-- meer bestaat. BEWUST apart van core.gemeentegrens: die tabel voedt
-- ponsenkaart-stats en mag geen overlappende (opgeheven) gemeenten bevatten.
-- Idempotent.
-- ============================================================================

CREATE TABLE IF NOT EXISTS core.gemeentegrens_historisch (
    overheidscode TEXT PRIMARY KEY,
    naam          TEXT NULL,
    jaar          INT  NULL,
    geometrie     GEOMETRY(MultiPolygon, 28992) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gemeentegrens_hist_geom
    ON core.gemeentegrens_historisch USING GIST(geometrie);
