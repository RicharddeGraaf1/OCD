-- ============================================================================
-- 2026-07 · v2a.chunk_annotatie — per-chunk (chunk × object × werkingsgebied)-
--           feiten-tabel voor de vectorindex.  FASE 1 (pure add-on, geen re-embed).
--
-- Materialiseert de drieslag tekst↔object↔locatie (annotatie / inline_ref /
-- naammatch) + een regelingsgebied-fallback op chunk-niveau, zodat een chunk
-- filterbaar wordt op zijn ECHTE werkingsgebied i.p.v. op de grove
-- regelingsgebied-scope, en zijn objecten kan tonen.
--
-- Ontwerp: analysis/Per-chunk werkingsgebied- en objectkoppeling in de vectorindex.md
-- Herkomst-conventie + waarom-tripel: zie die pagina §3/§4.
--
-- FASE 1: gekeyed op de BESTAANDE v2a.tekst_embedding(id). In Fase 3 migreert
-- dit naar de canonieke v2a.chunk. Non-breaking: raakt tekst_embedding niet.
--
-- Correctheid t.o.v. de v5-consistentie-view: de annotatie-join scope't hier
-- EXPLICIET op regeling_expression (jr.regeltekst_wid is niet globaal uniek —
-- wId-fan-out, zie gaps G-86). De v5-view deed dat niet.
--
-- Vertrouwens-differentiatie (ontwerp §4.4): inline_ref levert een BETROUWBAAR
-- gebied maar een ONBETROUWBAAR object bij gestapelde basisgeo (v5-caveat/G-71),
-- dus inline_ref schrijft alleen de locatie (object NULL). Objecten komen uit
-- annotatie (exact) en naammatch (zwak).
--
-- Run:  python scripts/run_sql.py scripts/2026-07-add-chunk-annotatie.sql
--   of: via psycopg (zie coverage-rapport in dezelfde sessie).
-- Idempotent: DROP + CREATE + TRUNCATE-via-recreate.
-- ============================================================================

DROP TABLE IF EXISTS v2a.chunk_annotatie;

CREATE TABLE v2a.chunk_annotatie (
    chunk_id     BIGINT NOT NULL REFERENCES v2a.tekst_embedding(id) ON DELETE CASCADE,
    object_type  TEXT,          -- activiteit | gebiedsaanwijzing | norm | omgevingswaarde | NULL
    object_id    TEXT,          -- p2p object-identificatie (NULL als alleen locatie bekend)
    locatie_id   TEXT,          -- p2p.locatie.identificatie (NULL bij begrip / naammatch-only)
    herkomst     TEXT NOT NULL, -- annotatie | inline_ref | naammatch | regelingsgebied
    kwalificatie TEXT
);

-- ---------------------------------------------------------------------------
-- 1. ANNOTATIE — Activiteit (via activiteit_locatieaanduiding; draagt object EN locatie)
--    activiteit_id is nullable (Instructie-/Omgevingswaarderegel) → dan object NULL,
--    locatie wél bekend = geldig "alleen-locatie"-feit.
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id,
    CASE WHEN ala.activiteit_id IS NOT NULL THEN 'activiteit' END,
    ala.activiteit_id,
    ala.locatie_id,
    'annotatie',
    ala.kwalificatie
FROM v2a.tekst_embedding v
JOIN p2p.tekst_element te            ON te.id = v.tekst_element_id
JOIN p2p.juridische_regel jr         ON jr.regeltekst_wid = te.wid
                                    AND jr.regeling_expression = te.regeling_expression
JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie;

-- ---------------------------------------------------------------------------
-- 2. ANNOTATIE — Gebiedsaanwijzing (via jrg → ga; ga draagt locatie)
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id, 'gebiedsaanwijzing', ga.identificatie, ga.locatie_id, 'annotatie', ga.type
FROM v2a.tekst_embedding v
JOIN p2p.tekst_element te     ON te.id = v.tekst_element_id
JOIN p2p.juridische_regel jr  ON jr.regeltekst_wid = te.wid
                             AND jr.regeling_expression = te.regeling_expression
JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jrg.gebiedsaanwijzing_id;

-- ---------------------------------------------------------------------------
-- 3. ANNOTATIE — Norm/Omgevingswaarde (via jrn → norm → normwaarde; nw draagt locatie)
--    Dit is de paring "welke norm op welk gebied" (11m↔Centrum / 8m↔Schil).
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id,
    CASE WHEN n.norm_type = 'Omgevingswaarde' THEN 'omgevingswaarde' ELSE 'norm' END,
    n.identificatie,
    nw.locatie_id,
    'annotatie',
    COALESCE(nw.kwalitatieve_waarde, nw.kwantitatieve_waarde::text)
FROM v2a.tekst_embedding v
JOIN p2p.tekst_element te     ON te.id = v.tekst_element_id
JOIN p2p.juridische_regel jr  ON jr.regeltekst_wid = te.wid
                             AND jr.regeling_expression = te.regeling_expression
JOIN p2p.juridische_regel_norm jrn ON jrn.juridische_regel_id = jr.identificatie
JOIN p2p.norm n              ON n.identificatie = jrn.norm_id
LEFT JOIN p2p.normwaarde nw  ON nw.norm_id = n.identificatie;

-- ---------------------------------------------------------------------------
-- 4. INLINE_REF — alleen locatie (betrouwbaar gebied, object bewust NULL; §4.4)
--    KRITISCH: gebruik de EXACTE set-match p2p.locatie_gio_match, NIET de rauwe
--    gio_basisgeo→locatie_basisgeo-join. Die laatste koppelt een chunk aan élke
--    locatie die één basisgeo-stukje met de GIO deelt → gemeten 750.802 rijen /
--    395.054 locaties (fan-out, betekenisloos). De exact-match (bg_count gelijk)
--    geeft de locatie wiens gebied IS de GIO → ~11.857 rijen / ~2,5 locatie/chunk.
--    GIO's zonder exacte match (~19%) vallen terug op regelingsgebied (stap 6).
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id, NULL, NULL, m.locatie_id, 'inline_ref', NULL
FROM v2a.tekst_embedding v
JOIN p2p.tekst_inline_referentie tir ON tir.tekst_element_id = v.tekst_element_id
                                    AND tir.soort = 'IntIoRef'
JOIN p2p.locatie_gio_match m ON m.gio_frbr = tir.target_gio_expression;

-- ---------------------------------------------------------------------------
-- 5. NAAMMATCH — alleen object (zwak signaal, locatie NULL)
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id,
    CASE ns.object_type
        WHEN 'Activiteit'       THEN 'activiteit'
        WHEN 'Gebiedsaanwijzing' THEN 'gebiedsaanwijzing'
        WHEN 'Omgevingsnorm'    THEN 'norm'
        WHEN 'Omgevingswaarde'  THEN 'omgevingswaarde'
        ELSE lower(ns.object_type)
    END,
    ns.object_id, NULL, 'naammatch', NULL
FROM v2a.tekst_embedding v
JOIN p2p.naammatch_signaal_intra ns ON ns.tekst_element_id = v.tekst_element_id;

-- ---------------------------------------------------------------------------
-- 6. REGELINGSGEBIED-fallback — grof gebied voor NIET-Begrip chunks die nog
--    geen enkel locatie-dragend feit kregen. Garandeert dat elke normatieve
--    chunk minstens een coarse werkingsgebied heeft. Begrippen krijgen bewust
--    GEEN gebied (§4.2 — definities zijn regeling-breed, apart bij de query).
-- ---------------------------------------------------------------------------
INSERT INTO v2a.chunk_annotatie (chunk_id, object_type, object_id, locatie_id, herkomst, kwalificatie)
SELECT DISTINCT
    v.id, NULL, NULL, r.regelingsgebied_id, 'regelingsgebied', NULL
FROM v2a.tekst_embedding v
JOIN p2p.regeling r ON r.frbr_expression = v.regeling_expression
WHERE v.bron_soort <> 'Begrip'
  AND r.regelingsgebied_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM v2a.chunk_annotatie ca
      WHERE ca.chunk_id = v.id AND ca.locatie_id IS NOT NULL
  );

-- ---------------------------------------------------------------------------
-- Indices
-- ---------------------------------------------------------------------------
CREATE INDEX chunk_annotatie_locatie_idx ON v2a.chunk_annotatie (locatie_id);
CREATE INDEX chunk_annotatie_chunk_idx   ON v2a.chunk_annotatie (chunk_id);
CREATE INDEX chunk_annotatie_object_idx  ON v2a.chunk_annotatie (object_type, object_id);
CREATE INDEX chunk_annotatie_herkomst_idx ON v2a.chunk_annotatie (herkomst);

ANALYZE v2a.chunk_annotatie;
