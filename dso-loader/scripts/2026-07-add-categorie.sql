-- ============================================================================
-- 2026-07 · v2a.categorie + v2a.chunk_categorie — de onderwerp-as (derde facet).
--
-- Ontwerp: analysis/Categorie-destillatie uit chunks - bottom-up met gecureerde lijst.md
--
-- v2a.categorie = de GECUREERDE GENERIEKE LIJST (first-class, compounding). Wordt
--   geseed uit de IMOW-thema-waardelijst (gebalanceerde ruggengraat) en gevoed door
--   bottom-up discovery (kandidaten). Reconcile mapt wegwerp-clusters → stabiele id.
-- v2a.chunk_categorie = droppable nearest-centroïde-cache (toewijzing per chunk).
--
-- Populatie via scripts/build_categorie.py (embeddings + clustering + reconcile).
-- Idempotent: DROP + CREATE.
-- ============================================================================

DROP TABLE IF EXISTS v2a.chunk_categorie;
DROP TABLE IF EXISTS v2a.categorie;

CREATE TABLE v2a.categorie (
    categorie_id     TEXT PRIMARY KEY,   -- stabiel, overleeft re-runs ('geluid', 'water.grondwateronttrekking')
    naam             TEXT,
    parent_id        TEXT REFERENCES v2a.categorie(categorie_id),  -- NULL=hoofd; anders subcategorie
    centroide        vector(768),        -- representatieve embedding → nearest-centroïde-toewijzing
    n_chunks_seen    INT DEFAULT 0,      -- support: hoeveel chunks droegen bij
    status           TEXT NOT NULL,      -- kandidaat | bevestigd | afgekeurd
    merged_into      TEXT,               -- als kandidaat → bestaande categorie ging
    bron             TEXT,               -- imow-thema | skos | discovery | gebruiker
    bron_run         TEXT,               -- welke discovery-run 'm voorstelde
    taxonomie_versie TEXT
);

CREATE TABLE v2a.chunk_categorie (
    chunk_id         BIGINT NOT NULL REFERENCES v2a.tekst_embedding(id) ON DELETE CASCADE,
    categorie_id     TEXT NOT NULL REFERENCES v2a.categorie(categorie_id),
    afstand          REAL,               -- cosine-afstand → herdrempelen zonder herrekenen
    taxonomie_versie TEXT
);
CREATE INDEX chunk_categorie_chunk_idx ON v2a.chunk_categorie (chunk_id);
CREATE INDEX chunk_categorie_cat_idx   ON v2a.chunk_categorie (categorie_id);
