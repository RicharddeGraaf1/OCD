-- ============================================================================
-- 2026-06 · naam_informatieobject op p2p.geo_informatieobject
--
-- Voegt de officiële `naamInformatieObject` (uit IO-<uuid>/Metadata.xml in de
-- Download-ZIP) toe als aparte, schone kolom. NIET te verwarren met de
-- bestaande kolom `naam`: die is een door de loader gesynthetiseerd UI-label
-- (binnenste locatie-/groep-labels, met ' / ' samengevoegd) en is voor
-- naam-match onbruikbaar. `naam_informatieobject` is een enkelvoudige naam
-- ("bed & breakfast", "Centrumgebied", "voetpaden") en wél geschikt voor de
-- drieslag-naam-match (type 3) — basis voor de GIO-tak in naammatch_signaal
-- en daarmee voor odkwaliteit-richtlijn H44 (vergeten GIO-referentie).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-gio-naam-informatieobject.sql
-- ============================================================================

ALTER TABLE p2p.geo_informatieobject
    ADD COLUMN IF NOT EXISTS naam_informatieobject TEXT NULL;

COMMENT ON COLUMN p2p.geo_informatieobject.naam_informatieobject IS
    'Officiële naamInformatieObject uit InformatieObjectMetadata (Download-ZIP). '
    'Schone enkelvoudige naam, geschikt voor naam-match — i.t.t. de kolom `naam` '
    '(gesynthetiseerd UI-label).';

-- Trigram-index voor de naam-match (word-boundary ILIKE in naammatch_signaal).
CREATE INDEX IF NOT EXISTS idx_gio_naam_io_trgm
    ON p2p.geo_informatieobject USING gin (naam_informatieobject gin_trgm_ops)
    WHERE naam_informatieobject IS NOT NULL;
