-- 2026-09-02 — Trefwoorden per werkzaamheid opslaan.
--
-- De RTR levert per werkzaamheid een `trefwoorden`-array: synoniemen waarop een
-- initiatiefnemer zoekt. `BedrijfDatAfvalwaterZuivert` draagt bijvoorbeeld
-- awzi, rwzi, rioolwaterzuivering, bedrijfsafvalwaterzuivering.
--
-- De eerste versie van de junctie-loader (2026-09-02) bewaarde alleen urn+naam.
-- Dat is precies de zoekindex die een werkzaamheid-ingang nodig heeft, en hij
-- komt gratis mee in de lijst-call die we toch al doen.
--
-- IDEMPOTENT. Draaien: psql "$DB_URL" -v ON_ERROR_STOP=1 -f 2026-09-add-werkzaamheid-trefwoorden.sql

BEGIN;

ALTER TABLE i2a.werkzaamheid
    ADD COLUMN IF NOT EXISTS trefwoorden text[];

COMMENT ON COLUMN i2a.werkzaamheid.trefwoorden IS
    'Synoniemen uit de RTR, bedoeld als zoekingang voor de initiatiefnemer.';

-- Zoeken op naam OF trefwoord, accent- en hoofdletterongevoelig.
CREATE INDEX IF NOT EXISTS idx_werkzaamheid_trefwoorden
    ON i2a.werkzaamheid USING gin (trefwoorden);
CREATE INDEX IF NOT EXISTS idx_werkzaamheid_naam_trgm
    ON i2a.werkzaamheid USING gin (lower(naam) gin_trgm_ops);

COMMIT;
