-- ============================================================================
-- 2026-11-12 · p2p.locatie_rand — de omtrek van elke Ow-locatie, vereenvoudigd
-- per zoomniveau, voor de randen in de vector-tile-laag.
--
-- WAAROM EEN EIGEN TABEL. De tegellaag tekende alleen een vulling, geen rand.
-- Een rand om de opgeslagen geometrie kan niet: p2p.locatie_subdiv en de
-- generalisatie die daarvan is afgeleid bestaan uit ST_Subdivide-stukjes, en
-- een tegel knipt elk vlak nog eens af op de tegelgrens. Een rand om elk stukje
-- tekent dus het hele knipraster mee (gemeten Veluwe z5: 396.000 randpunten,
-- vrijwel allemaal naden).
--
-- De echte omtrek is er wel: p2p.locatie draagt elk object in zijn geheel. Die
-- omtrek vereenvoudigen we hier één keer per niveau en slaan we op als lijnen.
-- Live doen kan niet: per tegel de hele provinciale vorm vereenvoudigen kostte
-- 297 s voor één schermbeeld. Per tegel de stukjes samenvoegen geeft hetzelfde
-- beeld maar kost op z5 7,7 s; vanaf z11 is dat wel goedkoop (0,2 s) en daar
-- doet ocd-api/tiles.py het ook live -- deze tabel dekt dus alleen z0-z10.
--
-- ST_Subdivide op de lijnen (max 256 punten per rij): zonder dat wordt de
-- omtrek van een provinciaal object één rij van honderdduizenden punten, die
-- elke tegel in de provincie in zijn geheel moet lezen.
--
-- Niveaus identiek aan p2p.locatie_generalisatie (hoogste bediende zoom):
--   niveau  6  → z0 t/m z6    tolerantie 53,76 m
--   niveau  8  → z7 en z8     tolerantie 13,44 m
--   niveau 10  → z9 en z10    tolerantie  3,36 m
--
-- Run:    psql ... -f scripts/2026-11-12-add-locatie-rand.sql
-- Vullen: PYTHONPATH=. .venv/Scripts/python scripts/vul_locatie_generalisatie.py --bron ow-rand
-- ============================================================================

CREATE TABLE IF NOT EXISTS p2p.locatie_rand (
    identificatie text     NOT NULL,
    niveau        smallint NOT NULL,
    geometrie     geometry(Geometry, 28992) NOT NULL,
    bron_hash     uuid     NOT NULL
);

COMMENT ON TABLE p2p.locatie_rand IS
    'Omtrek van p2p.locatie als lijnen, vereenvoudigd per niveau en opgedeeld in '
    'stukken van max 256 punten. Voor de randen in de vector tiles, z0-z10. '
    'Niveau = hoogste bediende zoom (6 = z<=6 @ 53,76 m; 8 = z7-8 @ 13,44 m; '
    '10 = z9-10 @ 3,36 m). Volledig herbouwbaar met '
    'scripts/vul_locatie_generalisatie.py --bron ow-rand.';

CREATE INDEX IF NOT EXISTS idx_locatie_rand_geom_n6
    ON p2p.locatie_rand USING gist (geometrie) WHERE niveau = 6;

CREATE INDEX IF NOT EXISTS idx_locatie_rand_geom_n8
    ON p2p.locatie_rand USING gist (geometrie) WHERE niveau = 8;

CREATE INDEX IF NOT EXISTS idx_locatie_rand_geom_n10
    ON p2p.locatie_rand USING gist (geometrie) WHERE niveau = 10;

CREATE INDEX IF NOT EXISTS idx_locatie_rand_id
    ON p2p.locatie_rand (identificatie, niveau);

-- Herbouw per bronhouder (`--bronhouder`, en zo in full_sync) selecteert op het
-- IMOW-prefix. Zie 2026-09-add-generalisatie-prefix-index.sql waarom dit
-- text_pattern_ops onder COLLATE "C" moet zijn en geen gewone btree.
CREATE INDEX IF NOT EXISTS idx_locatie_rand_id_prefix
    ON p2p.locatie_rand (identificatie COLLATE "C" text_pattern_ops, niveau);

-- De bron: p2p.locatie had alleen een unieke btree onder en_US-collatie, en
-- daarop kan `identificatie LIKE 'nl.imow-gm0995.%'` niet als prefix-bereik.
CREATE INDEX IF NOT EXISTS idx_locatie_id_prefix
    ON p2p.locatie (identificatie COLLATE "C" text_pattern_ops);
