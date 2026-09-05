-- ============================================================================
-- 2026-08 · v2a.wijziging_categorie — de onderwerp-as op WIJZIGINGEN.
--
-- Plan: c:/GIT/OCDviewer/docs/plans/wijzigingentour-op-onderwerp.md
-- Populatie: scripts/classify_wijziging.py
--
-- WAAROM DIT NODIG IS
--   De onderwerp-as (v2a.categorie + v2a.chunk_categorie) hangt aan de GELDENDE
--   regeling. Een categorie opzoeken via de wId van een gewijzigd artikel werkt
--   daardoor niet:
--     - de wId draagt een component-token dat per regelingversie wisselt. Bij
--       gm1586 (Ede) is de overlap tussen v2a-wIds en p2p-wIds exact nul, bij
--       gm0394 (Haarlemmermeer) 68%. /v1/.../onderwerpen snijdt met de wIds van
--       het document en geeft daar dus stil een leeg antwoord;
--     - een artikel dat in dít ontwerp nieuw is bestaat per definitie niet in de
--       geldende regeling — toegevoegde artikelen haalden 2,7% dekking tegen
--       13,1% voor gewijzigde.
--   Classificeren van de ONTWERP-tekst zelf omzeilt beide.
--
-- WAAROM GEEN EIGEN EMBEDDING-TABEL
--   Die bestaat al: run_overnight_ontwerp.py schrijft p2pwijziging-tekst als
--   source_type='ontwerp' in v2a.tekst_embedding (469.298 chunks over 240 van de
--   251 werken met wijzigingen), met dezelfde chunk-opbouw als de p2p-laag
--   (kop_pad · inhoud_plain, Lid/Divisietekst/Begrip + Artikel-zonder-Lid,
--   length > 30, nomic-embed-text). Die vectoren zijn dus direct vergelijkbaar
--   met de centroïden in v2a.categorie. Opnieuw embedden zou verspilling zijn.
--
-- WAAROM DAN TOCH EEN EIGEN TOEWIJZINGSTABEL, EN NIET v2a.chunk_categorie
--   Twee redenen, allebei hard:
--     1. build_categorie.py DROPt v2a.chunk_categorie bij elke herbouw. Alles wat
--        we daar bijschrijven is bij de volgende taxonomie-run weg.
--     2. Ontwerp-chunks dragen regeling_expression = regeling_work (een ontwerp
--        heeft nog geen expressie). Het /onderwerpen-endpoint filtert op
--        split_part(regeling_expression,'/nld@',1) = werk — ontwerp-rijen MATCHEN
--        daar dus. Zolang ze geen chunk_categorie hebben blijven ze onzichtbaar;
--        zodra we ze daar bijschrijven zou ontwerp-tekst stilletjes als onderwerp
--        van het GELDENDE plan gaan verschijnen.
--
-- KOPPELING OP wId, NIET OP source_ref
--   run_overnight_ontwerp.py bewaart source_ref = p2pwijziging.tekst_element.id.
--   Die tabel wordt bij elke ontwerp-load opnieuw gevuld met verse BIGSERIALs, dus
--   die verwijzing veroudert: voor gm0394 resolveert nog 2.182 van de 4.555. De
--   wId matcht daar wél 100%. Alle koppelingen hieronder lopen daarom over
--   (regeling_work, wid).
--
-- Idempotent: CREATE IF NOT EXISTS + CREATE OR REPLACE VIEW.
-- ============================================================================

CREATE TABLE IF NOT EXISTS v2a.wijziging_categorie (
    chunk_id          BIGINT NOT NULL
                      REFERENCES v2a.tekst_embedding(id) ON DELETE CASCADE,
    regeling_work     TEXT NOT NULL,   -- gedenormaliseerd: scoping zonder join
    wid               TEXT,            -- wId van de chunk zelf
    artikel_wid       TEXT,            -- dichtstbijzijnde Artikel-voorouder; de tour-sleutel
    categorie_id      TEXT NOT NULL REFERENCES v2a.categorie(categorie_id),
    afstand           REAL,            -- 1 - cosine; herdrempelen zonder herrekenen
    taxonomie_versie  TEXT
);

-- Eén toewijzing per chunk. Maakt de loader idempotent via ON CONFLICT en
-- voorkomt stapeling bij een half afgebroken run.
CREATE UNIQUE INDEX IF NOT EXISTS wijziging_categorie_chunk_uidx
    ON v2a.wijziging_categorie (chunk_id);
CREATE INDEX IF NOT EXISTS wijziging_categorie_werk_idx
    ON v2a.wijziging_categorie (regeling_work);
CREATE INDEX IF NOT EXISTS wijziging_categorie_artikel_idx
    ON v2a.wijziging_categorie (regeling_work, artikel_wid);
CREATE INDEX IF NOT EXISTS wijziging_categorie_cat_idx
    ON v2a.wijziging_categorie (categorie_id);

-- Rollup naar artikel-niveau. De keuze "welk onderwerp krijgt dit artikel" hoort
-- hier en niet in drie consumenten: DISTINCT ON + ORDER BY afstand laat het
-- zekerste onderwerp winnen.
--
-- Grain = (regeling_work, artikel_wid), niet per besluit: ontwerp-chunks dragen
-- alleen het werk. Twee ontwerpen die hetzelfde artikel anders wijzigen delen dus
-- één onderwerp. Dat is aanvaardbaar — een onderwerp is een eigenschap van waar
-- het artikel over gaat, niet van de delta.
CREATE OR REPLACE VIEW v2a.wijziging_artikel_categorie AS
SELECT DISTINCT ON (w.regeling_work, w.artikel_wid)
       w.regeling_work,
       w.artikel_wid,
       w.categorie_id,
       k.naam                   AS categorie,
       coalesce(p.naam, k.naam) AS hoofdcategorie,
       (k.parent_id IS NULL)    AS is_hoofd,
       w.afstand,
       w.taxonomie_versie
FROM v2a.wijziging_categorie w
JOIN v2a.categorie k ON k.categorie_id = w.categorie_id
LEFT JOIN v2a.categorie p ON p.categorie_id = k.parent_id
WHERE w.artikel_wid IS NOT NULL
ORDER BY w.regeling_work, w.artikel_wid, w.afstand;
