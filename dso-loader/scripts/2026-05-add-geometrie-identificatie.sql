-- ============================================================================
-- 2026-05 · Geometrie-identificatie kolommen op locatie en geo_informatieobject
--
-- Werkblok 1 van [[Plan implementatie GIO-laden]] §"Optie C-light".
--
-- Doel: koppeling Locatie ↔ GIO via gedeelde geometrie-UUID
-- (`geometrieIdentificatie`). De UUID zit al in de Presenteren-API-payload
-- van Locaties (`loc["geometrieIdentificatie"]`, gebruikt voor
-- `/geometrieen/{uuid}`-calls in api_loader.py) en in de GIO-metadata-XML
-- in de Download-ZIP. Tot nu toe werd hij in geen van beide tabellen
-- opgeslagen.
--
-- Effect: stap-3-consistentie-view kan straks via deze UUID koppelen
-- in plaats van via `p2p.juridische_borging` (G-69), die optioneel is
-- en niet wordt geladen.
--
-- Achtergrond: zie [[gaps]] G-69 en [[Plan implementatie GIO-laden]].
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-geometrie-identificatie.sql
-- ============================================================================

ALTER TABLE p2p.locatie
    ADD COLUMN IF NOT EXISTS geometrie_identificatie TEXT NULL;

ALTER TABLE p2p.geo_informatieobject
    ADD COLUMN IF NOT EXISTS geometrie_identificatie TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_locatie_geom_id
    ON p2p.locatie (geometrie_identificatie)
    WHERE geometrie_identificatie IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_gio_geom_id
    ON p2p.geo_informatieobject (geometrie_identificatie)
    WHERE geometrie_identificatie IS NOT NULL;

COMMENT ON COLUMN p2p.locatie.geometrie_identificatie IS
    'UUID van de geometrie (uit Presenteren `loc.geometrieIdentificatie`). '
    'Gedeeld met p2p.geo_informatieobject.geometrie_identificatie via dezelfde '
    'fysieke geometrie — koppel-key voor de drieslag-consistentie-view.';

COMMENT ON COLUMN p2p.geo_informatieobject.geometrie_identificatie IS
    'UUID van de geometrie (uit GIO-metadata-XML in Download-ZIP). Gedeeld '
    'met p2p.locatie.geometrie_identificatie. Wordt door GIO-loader gevuld; '
    'voor rijen die via optie A uit ExtIoRef.target_ref zijn afgeleid is dit '
    'veld NULL totdat de GIO-loader heeft gedraaid.';
