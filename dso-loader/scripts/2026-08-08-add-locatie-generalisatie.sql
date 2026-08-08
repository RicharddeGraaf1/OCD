-- ============================================================================
-- 2026-08-08 · p2p.locatie_generalisatie — voorberekende, vereenvoudigde
-- geometrie voor de vector-tile-laag (zie OCDviewer docs/plans/vector-tiles.md).
--
-- Waarom een aparte tabel en geen kolom op p2p.locatie_subdiv:
--   * ijl      — op grof niveau valt een deel van de vlakken weg (sub-pixel)
--   * wegwerp  — volledig herbouwbaar zonder de brontabel aan te raken
--   * isolatie — houdt de 12 GB-brontabel buiten de herbouw
--
-- Niveaus zijn genoemd naar het HOOGSTE zoomniveau dat ze bedienen, zodat de
-- keuze server-side leesbaar blijft:
--
--   niveau  6  → z0 t/m z6    tolerantie 53,76 m   (resolutie op z6)
--   niveau  8  → z7 en z8     tolerantie 13,44 m   (resolutie op z8)
--   niveau 10  → z9 en z10    tolerantie  3,36 m   (resolutie op z10)
--   (vanaf z11 rechtstreeks uit p2p.locatie_subdiv)
--
-- Elk niveau bedient twee zoomstappen. Meer stappen per niveau kan niet: een
-- niveau dat op z8 is afgestemd laat op z6 nog 57.469 vlakken in een tegel
-- staan (1,9 MB), want de sub-pixelgrens van z8 is vier keer fijner dan die
-- van z6. Gemeten, zie docs/plans/vector-tiles.md.
--
-- De resoluties komen uit de PDOK-RD-piramide die de viewer al gebruikt:
-- 3440.640 m/px op z0, elke stap gehalveerd.
--
-- bron_hash is de vingerafdruk van de BRON-geometrie op het moment van
-- berekenen (md5 van de WKB, als uuid = 16 bytes i.p.v. 33 als text). Daarmee
-- is na een sync-run in één query te zien welke rijen achterlopen — en is een
-- incrementele herbouw later mogelijk zonder schemawijziging.
--
-- Run: psql ... -f scripts/2026-08-08-add-locatie-generalisatie.sql
-- Vullen: PYTHONPATH=. .venv/Scripts/python scripts/vul_locatie_generalisatie.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS p2p.locatie_generalisatie (
    identificatie text     NOT NULL,
    niveau        smallint NOT NULL,
    geometrie     geometry(Geometry, 28992) NOT NULL,
    bron_hash     uuid     NOT NULL
);

COMMENT ON TABLE p2p.locatie_generalisatie IS
    'Vereenvoudigde weergave-afgeleide van p2p.locatie_subdiv voor vector tiles. '
    'Niveau = hoogste bediende zoom (6 = z<=6 @ 53,76 m; 8 = z7-8 @ 13,44 m; '
    '10 = z9-10 @ 3,36 m). Volledig herbouwbaar met '
    'scripts/vul_locatie_generalisatie.py.';

COMMENT ON COLUMN p2p.locatie_generalisatie.bron_hash IS
    'md5(ST_AsBinary(brongeometrie))::uuid ten tijde van de berekening.';

-- Tegelquery is altijd: WHERE niveau = N AND geometrie && envelope.
-- Partiele GIST-indexen per niveau houden elke index klein en maken het
-- niveau-filter gratis.
CREATE INDEX IF NOT EXISTS idx_locatie_gen_geom_n6
    ON p2p.locatie_generalisatie USING gist (geometrie) WHERE niveau = 6;

CREATE INDEX IF NOT EXISTS idx_locatie_gen_geom_n8
    ON p2p.locatie_generalisatie USING gist (geometrie) WHERE niveau = 8;

CREATE INDEX IF NOT EXISTS idx_locatie_gen_geom_n10
    ON p2p.locatie_generalisatie USING gist (geometrie) WHERE niveau = 10;

-- Voor de koppeling tegel-feature -> object (naam, aanvinken) en voor de
-- drift-controle tegen de bron.
CREATE INDEX IF NOT EXISTS idx_locatie_gen_id
    ON p2p.locatie_generalisatie (identificatie, niveau);
