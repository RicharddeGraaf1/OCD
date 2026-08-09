-- ============================================================================
-- 2026-08-09 · wro.planobject_generalisatie — de Wro-tegenhanger van
-- p2p.locatie_generalisatie. Zie 2026-08-08-add-locatie-generalisatie.sql voor
-- de redenering; hier alleen wat aan deze kant anders is.
--
-- 1. GEEN OPGEDEELDE BRON. p2p.locatie_subdiv is de opgedeelde variant van de
--    Ow-locaties; wro.planobject draagt de hele vorm en heeft een primaire
--    sleutel. Er zijn dus geen deelnaden.
-- 2. NIET ALLEEN VLAKKEN. Ongeveer 5% van de planobjecten is een lijn (de
--    Figuur-objecten: gevellijnen, bouwvlakgrenzen). De sub-pixeltoets op de
--    bounding box werkt daar net zo goed -- een lijn die binnen een pixel past
--    is even onzichtbaar als een vlakje -- maar de kaartlaag moet ze wél als
--    lijn tekenen, want een lijn heeft geen vulling.
--
-- Niveaus identiek aan de Ow-kant (hoogste bediende zoom):
--   niveau  6  → z0 t/m z6    tolerantie 53,76 m
--   niveau  8  → z7 en z8     tolerantie 13,44 m
--   niveau 10  → z9 en z10    tolerantie  3,36 m
--   (vanaf z11 rechtstreeks uit wro.planobject)
--
-- Run: psql ... -f scripts/2026-08-09-add-planobject-generalisatie.sql
-- Vullen: PYTHONPATH=. .venv/Scripts/python scripts/vul_locatie_generalisatie.py --bron wro
-- ============================================================================

CREATE TABLE IF NOT EXISTS wro.planobject_generalisatie (
    identificatie text     NOT NULL,
    niveau        smallint NOT NULL,
    geometrie     geometry(Geometry, 28992) NOT NULL,
    bron_hash     uuid     NOT NULL
);

COMMENT ON TABLE wro.planobject_generalisatie IS
    'Vereenvoudigde weergave-afgeleide van wro.planobject voor vector tiles. '
    'Niveau = hoogste bediende zoom (6 = z<=6 @ 53,76 m; 8 = z7-8 @ 13,44 m; '
    '10 = z9-10 @ 3,36 m). Bevat vlakken en lijnen. Volledig herbouwbaar met '
    'scripts/vul_locatie_generalisatie.py --bron wro.';

CREATE INDEX IF NOT EXISTS idx_planobject_gen_geom_n6
    ON wro.planobject_generalisatie USING gist (geometrie) WHERE niveau = 6;

CREATE INDEX IF NOT EXISTS idx_planobject_gen_geom_n8
    ON wro.planobject_generalisatie USING gist (geometrie) WHERE niveau = 8;

CREATE INDEX IF NOT EXISTS idx_planobject_gen_geom_n10
    ON wro.planobject_generalisatie USING gist (geometrie) WHERE niveau = 10;

CREATE INDEX IF NOT EXISTS idx_planobject_gen_id
    ON wro.planobject_generalisatie (identificatie, niveau);
