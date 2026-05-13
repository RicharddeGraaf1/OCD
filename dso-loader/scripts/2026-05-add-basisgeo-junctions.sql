-- ============================================================================
-- 2026-05 · basisgeo-junctions voor Locatie ↔ GIO koppeling
--
-- Refactor van werkblok 1 in [[Plan implementatie GIO-laden]] §"Optie C-light".
--
-- Aanleiding (gebruiker, 2026-05-12): de eerste optie C-light-aanpak
-- (gedeelde UUID via `geometrie_identificatie`-kolom op locatie/GIO)
-- werkte niet omdat:
--   1. Presenteren v8 met `locatieSelectie=primair` levert per
--      Gebiedengroep een geaggregeerde geometrie met een EIGEN UUID,
--      niet de basisgeo:id van de individuele leden.
--   2. Een GIO-GML bevat meerdere `basisgeo:id` (één per Locatie binnen
--      het GIO), niet één.
--
-- Echte koppeling loopt via M:N junctions:
--   GA → Locatie (NEN3610) → basisgeo:id (uit gebieden.xml) →
--     GIO (uit GIO-GML met dezelfde basisgeo:id) ← IntIoRef target
--
-- Voor Gebiedengroepen: transitief via Gebied-leden (uit gebiedengroepen.xml).
--
-- Idempotent. Run:
--   psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-basisgeo-junctions.sql
-- ============================================================================

-- Geen FK op locatie_id: `gebieden.xml` bevat ook individuele Gebieden die
-- niet via Presenteren (locatieSelectie=primair) in p2p.locatie terechtkomen
-- — alleen Gebiedengroep-aggregaten zitten daar. De junction-tabel mag
-- dekkender zijn dan p2p.locatie; bij JOIN-time vallen niet-bestaande
-- locaties automatisch weg.
CREATE TABLE IF NOT EXISTS p2p.locatie_basisgeo (
    locatie_id   TEXT NOT NULL,
    basisgeo_id  TEXT NOT NULL,
    PRIMARY KEY (locatie_id, basisgeo_id)
);
CREATE INDEX IF NOT EXISTS idx_locatie_basisgeo_id
    ON p2p.locatie_basisgeo (basisgeo_id);

CREATE TABLE IF NOT EXISTS p2p.gio_basisgeo (
    gio_frbr     TEXT NOT NULL REFERENCES p2p.geo_informatieobject(frbr_expression) ON DELETE CASCADE,
    basisgeo_id  TEXT NOT NULL,
    PRIMARY KEY (gio_frbr, basisgeo_id)
);
CREATE INDEX IF NOT EXISTS idx_gio_basisgeo_id
    ON p2p.gio_basisgeo (basisgeo_id);

COMMENT ON TABLE p2p.locatie_basisgeo IS
    'M:N junction Locatie ↔ basisgeo:id. Voor Gebied: één rij (haar eigen '
    'GeometrieRef). Voor Gebiedengroep: transitief via lid-Gebieden uit '
    'gebiedengroepen.xml. Bron: OW-bestanden/gebieden.xml + gebiedengroepen.xml '
    'in de Download-ZIP.';

COMMENT ON TABLE p2p.gio_basisgeo IS
    'M:N junction GIO ↔ basisgeo:id. Een GIO-GML bevat één of meer '
    '`basisgeo:id`-elementen, één per Locatie binnen het GIO. Bron: '
    'IO-*/*.gml-bestanden in de Download-ZIP.';
