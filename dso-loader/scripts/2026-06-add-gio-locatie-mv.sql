-- ============================================================================
-- 2026-06 · p2p.gio_locatie — gematerialiseerde GIO ↔ Locatie koppeling
--
-- Aanleiding (gebruiker, 2026-06-09/10): de koppeling GIO ↔ locatie is een
-- dubbele M:N-junction op basisgeo_id (gio_basisgeo ⋈ locatie_basisgeo). Dat
-- elke query opnieuw joinen is duur; deze matview materialiseert de distinct
-- (gio_frbr, locatie_id)-paren één keer.
--
-- Gefilterd op locaties die ECHT in p2p.locatie staan: locatie_basisgeo is
-- denser (bevat ~61% losse Gebieden uit gebieden.xml die niet als zelfstandige
-- locatie geladen zijn). Door hier op p2p.locatie te joinen is de matview
-- direct bruikbaar voor de objectlijst-endpoints zonder phantom-IDs.
--
-- Dekking (2026-06): 263.821/312.397 locaties (84%) koppelen aan >=1 GIO.
--
-- Refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.gio_locatie;
--   (vereist de unieke index hieronder). Opgenomen in refresh_drieslag.py.
-- Idempotent: IF NOT EXISTS op matview en indices.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-gio-locatie-mv.sql
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS p2p.gio_locatie AS
SELECT DISTINCT
    gb.gio_frbr,
    lb.locatie_id
FROM p2p.gio_basisgeo gb
JOIN p2p.locatie_basisgeo lb ON lb.basisgeo_id = gb.basisgeo_id
JOIN p2p.locatie l ON l.identificatie = lb.locatie_id;

-- Unieke index: nodig voor REFRESH ... CONCURRENTLY én dedup-garantie.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gio_locatie_pk
    ON p2p.gio_locatie (gio_frbr, locatie_id);
CREATE INDEX IF NOT EXISTS idx_gio_locatie_loc
    ON p2p.gio_locatie (locatie_id);
CREATE INDEX IF NOT EXISTS idx_gio_locatie_gio
    ON p2p.gio_locatie (gio_frbr);

COMMENT ON MATERIALIZED VIEW p2p.gio_locatie IS
    'Distinct (gio_frbr, locatie_id) via gio_basisgeo ⋈ locatie_basisgeo, '
    'gefilterd op bestaande p2p.locatie. Vervangt de live dubbel-join voor '
    'GIO↔Locatie. Refresh via refresh_drieslag.py.';
