-- ============================================================================
-- 2026-08 · v2a.pad_categorie + v2a.artikel_indeling — de indeling als OPZOEKING.
--
-- Ontwerp: docs/onderwerp-as-en-typebepaling-as.md
-- Vault:   analysis/Onderwerp-as en typeBepaling-as in de machinale categorie-indeling.md
--
-- Waarom dit naast v2a.categorie staat en die niet vervangt: `v2a.categorie` +
-- `chunk_categorie` blijven de vector-retrieval bedienen. Deze twee tabellen
-- bedienen de UI, en doen dat zonder model:
--
--   categorie/subcategorie  <- genormaliseerd VOOROUDERPAD  (handcuratie)
--   type_bepaling           <- genormaliseerd ARTIKELOPSCHRIFT (gesloten lexicon)
--
-- Twee assen, niet twee niveaus. Het pad zegt waar een bepaling OVER gaat,
-- het artikelopschrift zegt wat voor bepaling het IS. Die twee zaten tot nu toe
-- op één as gepropt, waardoor "toepassingsbereik" een subcategorie werd onder
-- de naam "Tanken en vloeibare brandstoffen".
--
-- NULL betekent NIET INGEDEELD en is een geldig antwoord (gebruiker 2026-08-09):
-- geen sleuteltreffer -> geen waarde. Niet raden, niet naar de dichtstbijzijnde
-- buur schuiven. De UI toont dat als een grijze "niet ingedeeld (n)"-knop.
--
-- Populatie via scripts/bouw_indeling.py. Idempotent: DROP + CREATE.
-- ============================================================================

DROP TABLE IF EXISTS v2a.artikel_indeling;
DROP TABLE IF EXISTS v2a.pad_categorie;

-- De gecureerde opzoektabel. Eén rij per genormaliseerd opschriftpad.
-- 10.798 ruwe paden vallen na normaliseren samen tot 9.999 sleutels; de
-- 1.000 grootste dekken 78,6% van alle artikelen.
CREATE TABLE v2a.pad_categorie (
    pad_sleutel      TEXT PRIMARY KEY,
    pad_voorbeeld    TEXT,          -- één ruwe schrijfwijze, zodat een mens 'm herkent
    categorie        TEXT,          -- NULL = bewust niet ingedeeld
    subcategorie     TEXT,
    n_artikelen      INT,           -- omvang bij het genereren van het werkblad
    n_bronhouders    INT,           -- spreiding; >= 50 = landelijk (bruidsschat)
    bron             TEXT,          -- 'curatie'
    curatie_versie   TEXT
);

-- De materialisatie: één rij per artikel.
CREATE TABLE v2a.artikel_indeling (
    tekst_element_id    BIGINT PRIMARY KEY REFERENCES p2p.tekst_element(id) ON DELETE CASCADE,
    regeling_expression TEXT NOT NULL,
    wid                 TEXT,
    pad_sleutel         TEXT,       -- de sleutel waarop is opgezocht
    categorie           TEXT,       -- NULL = niet ingedeeld
    subcategorie        TEXT,
    type_bepaling       TEXT,       -- NULL = niet herkend
    herkomst            TEXT,       -- 'pad-curatie' | 'artikelopschrift' | 'beide' | NULL
    curatie_versie      TEXT
);

CREATE INDEX artikel_indeling_regeling_idx ON v2a.artikel_indeling (regeling_expression);
CREATE INDEX artikel_indeling_wid_idx      ON v2a.artikel_indeling (wid);
CREATE INDEX artikel_indeling_cat_idx      ON v2a.artikel_indeling (categorie);
CREATE INDEX artikel_indeling_type_idx     ON v2a.artikel_indeling (type_bepaling);
CREATE INDEX artikel_indeling_pad_idx      ON v2a.artikel_indeling (pad_sleutel);
