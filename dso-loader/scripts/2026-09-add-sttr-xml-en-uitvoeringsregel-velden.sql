-- 2026-09-02 — Opslag van het ruwe STTR-bestand + de velden die de parser weggooide.
--
-- AANLEIDING (gaps#G-138)
-- `_parse_and_store_dmn` bewaart nu alleen Decision, InputData en een
-- tweewaardige regel_type. Gevolg: i2a.uitvoeringsregel heeft 1.238.206 rijen
-- waarvan `dmn_element_id`, `nen3610_id` en `activiteit_urn` 100% NULL zijn, en
-- `bereik` wordt in de parser wél uitgelezen maar niet weggeschreven.
--
-- De bron levert het wél. Gemeten op een gespreide steekproef van 360 bestanden
-- (2026-09-01): bereik 60,6% · bijlage 35,3% · vasteWaarde 36,7% ·
-- herbruikbaarId 12,8% · implicietAntwoord 6,1% · geoVerwijzing 5,8% ·
-- uitkomstHerbruikbareBeslissing 0,6%.
--
-- KERNKEUZE: NETWERK EN PARSEN SCHEIDEN
-- De XML wordt nu niet bewaard, dus elke parser-wijziging zou een nieuwe ronde
-- langs de API kosten (~52.500 calls). Daarom slaat `i2a.sttr_bestand` het ruwe
-- bestand gzipped op. Meting: factor 7,2 compressie, 2,65 GB → ~0,37 GB voor de
-- hele voorraad. Daarna is elke parser-fix een lokale herparse van nul calls.
--
-- Die tabel is tegelijk het checkpoint: wat erin staat is opgehaald.
--
-- Gekeyd op de STTR-id van het bestand, NIET op namespace. De namespace is niet
-- uniek per regelbestand (gaps#G-90) — meerdere bestanden delen er één, en in
-- i2a.toepasbaar_regelbestand (PK = namespace) vallen die dus samen. De id uit
-- het `self`-link is wel per bestand.
--
-- IDEMPOTENT: veilig herhaalbaar.
-- Draaien:  psql "$DB_URL" -v ON_ERROR_STOP=1 -f 2026-09-add-sttr-xml-en-uitvoeringsregel-velden.sql

BEGIN;

-- 1. Ruwe bestandsopslag = ophaal-checkpoint -------------------------------
CREATE TABLE IF NOT EXISTS i2a.sttr_bestand (
    sttr_id           text PRIMARY KEY,
    fsr               text,
    oin               text,
    sttr_versie       integer,
    laatste_wijziging text,
    bytes_rauw        integer,
    xml_gz            bytea NOT NULL,
    opgehaald_op      timestamptz NOT NULL DEFAULT now(),
    geparsed_op       timestamptz
);

COMMENT ON TABLE i2a.sttr_bestand IS
    'Ruw sttrBestand, gzipped. Tegelijk het checkpoint van de ophaalronde: wat '
    'hier staat is binnen. Gekeyd op de STTR-id per bestand, niet op namespace '
    '(die is niet uniek per regelbestand — gaps#G-90).';
COMMENT ON COLUMN i2a.sttr_bestand.geparsed_op IS
    'NULL = nog te parsen. Een parser-wijziging zet dit terug op NULL en kost '
    'geen enkele API-call.';

CREATE INDEX IF NOT EXISTS idx_sttr_bestand_fsr ON i2a.sttr_bestand (fsr);
CREATE INDEX IF NOT EXISTS idx_sttr_bestand_te_parsen
    ON i2a.sttr_bestand (sttr_id) WHERE geparsed_op IS NULL;

-- 2. De velden die de parser liet vallen -----------------------------------
ALTER TABLE i2a.uitvoeringsregel
    ADD COLUMN IF NOT EXISTS sttr_id       text,
    ADD COLUMN IF NOT EXISTS uitv_dmn_id   text,
    ADD COLUMN IF NOT EXISTS bereik        text,
    ADD COLUMN IF NOT EXISTS gegevens_type text;

COMMENT ON COLUMN i2a.uitvoeringsregel.bereik IS
    'werkzaamheid | locatie | gebruiker — binnen welk bereik het antwoord geldt '
    '(IMTR 3.0.1 §6). Kwam in 60,6% van de bestanden voor en werd weggegooid.';
COMMENT ON COLUMN i2a.uitvoeringsregel.uitv_dmn_id IS
    'De id uit het XML-element, bv. UitvId0002. Nodig om dmn:inputData te '
    'koppelen via uitv:uitvoeringsregelRef, en om herparsen idempotent te maken.';
COMMENT ON COLUMN i2a.uitvoeringsregel.nen3610_id IS
    'Locatie-identificatie uit uitv:geoVerwijzing/uitv:locatie/@identificatie — '
    'de brug IMTR -> CIM-OW Locatie (IMTR 3.0.1 §6.1.8).';
COMMENT ON COLUMN i2a.uitvoeringsregel.activiteit_urn IS
    'Activiteit-URN uit uitv:uitkomstHerbruikbareBeslissing/uitv:activiteit/@urn '
    '— de brug IMTR -> CIM-OW Activiteit (IMTR 3.0.1 §6.1.10).';
COMMENT ON COLUMN i2a.uitvoeringsregel.regel_type IS
    'Een van de tien typen uit IMTR 3.0.1 §6.1. Tot 2026-09-02 stonden hier maar '
    'twee waarden (Vraag / Uitvoeringsregel).';

-- Herparsen mag nooit duplicaten opleveren.
CREATE UNIQUE INDEX IF NOT EXISTS uq_uitv_sttr_dmn
    ON i2a.uitvoeringsregel (sttr_id, uitv_dmn_id)
    WHERE sttr_id IS NOT NULL AND uitv_dmn_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_uitv_nen3610
    ON i2a.uitvoeringsregel (nen3610_id) WHERE nen3610_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_uitv_activiteit_urn
    ON i2a.uitvoeringsregel (activiteit_urn) WHERE activiteit_urn IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_uitv_sttr_id
    ON i2a.uitvoeringsregel (sttr_id);

COMMIT;
