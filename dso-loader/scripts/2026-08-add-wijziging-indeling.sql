-- ============================================================================
-- 2026-08 · v2a.wijziging_indeling — dezelfde twee assen, nu op de renvooi-tekst.
--
-- Ontwerp: docs/onderwerp-as-en-typebepaling-as.md
--
-- Vervangt de rol van `v2a.wijziging_categorie` + `wijziging_artikel_categorie`
-- in de wijzigingentour. Die twee kwamen uit de centroïde-taxonomie en droegen
-- dus dezelfde asvermenging als het register vóór 2026-08-09: de grootste
-- "categorie" op wijzigingen was "Tanken en vloeibare brandstoffen" met 6.026
-- artikelen, gevolgd door drie waarden die in werkelijkheid typeBepaling zijn.
--
-- Twee routes naar een onderwerp, in deze volgorde:
--
--   1. REGISTER — de vigerende `v2a.artikel_indeling` op (work, wid). Voor een
--      bestaand artikel is dat het volledige, gezaghebbende opschriftpad, en het
--      houdt de tour consistent met wat het register bij hetzelfde artikel toont.
--   2. RENVOOI  — het opschriftpad uit de p2pwijziging-boom zelf. Dit is de enige
--      route die werkt voor NIEUWE artikelen, die per definitie nog niet in de
--      vigerende regeling staan (2.290 van 11.673 gemeten 2026-08-10).
--
-- Gemeten op 5.211 artikelen waar beide routes een antwoord geven: 99,7% gelijk.
-- De volgorde is dus een consistentie-keuze, geen correctheids-keuze.
--
-- GEEN foreign key naar p2pwijziging.tekst_element, en dat is opzet.
-- Die mirror wordt bij elke load opnieuw gevuld met verse BIGSERIALs — zie de
-- kop van classify_wijziging.py — dus een FK met ON DELETE CASCADE zou de
-- indeling bij elke sync stilzwijgend wegvagen. Precies de val die
-- v2a.artikel_indeling op 2026-08-10 bleek te hebben. De natuurlijke sleutel
-- (regeling_work, artikel_wid) veroudert niet.
--
-- Populatie via scripts/bouw_indeling.py (of --alleen-wijzigingen om alleen deze
-- tabel te herbouwen). Idempotent: DROP + CREATE.
-- ============================================================================

DROP TABLE IF EXISTS v2a.wijziging_indeling;

CREATE TABLE v2a.wijziging_indeling (
    regeling_work   TEXT NOT NULL,
    artikel_wid     TEXT NOT NULL,
    pad_sleutel     TEXT,        -- sleutel van het renvooi-pad, ook als route 1 won
    categorie       TEXT,        -- NULL = niet ingedeeld
    subcategorie    TEXT,
    type_bepaling   TEXT,        -- NULL = niet herkend
    herkomst        TEXT,        -- 'register' | 'renvooi' | NULL
    curatie_versie  TEXT,
    PRIMARY KEY (regeling_work, artikel_wid)
);

CREATE INDEX wijziging_indeling_cat_idx  ON v2a.wijziging_indeling (categorie);
CREATE INDEX wijziging_indeling_type_idx ON v2a.wijziging_indeling (type_bepaling);
