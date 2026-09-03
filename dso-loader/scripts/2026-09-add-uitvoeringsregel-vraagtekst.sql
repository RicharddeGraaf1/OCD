-- 2026-09-03 — De leesbare kant van een uitvoeringsregel opslaan.
--
-- AANLEIDING
-- Na de eerste backfill (gaps#G-138) staat er per uitvoeringsregel wel een
-- type, een bereik en een gegevenstype, maar niets wat een mens kan lezen.
-- Het enige tekstveld dat we hadden was de InputData-naam uit het DMN, en dat
-- is een variabelenaam, geen vraag:
--
--     keuze dakvlak plaatsen dakraam UR
--     bouwwerk oppervlakte minder dan 50 m2 UR
--
-- De echte tekst staat als CDATA in de XML die we sinds vandaag lokaal
-- bewaren, dus dit ophalen kost NUL API-calls: parser aanpassen en
-- `parse-sttr-xml --opnieuw` draaien.
--
-- WAT ER BIJKOMT
--   label        vraagTekst bij een Vraag, bijlageType bij een Bijlage — de
--                regel zoals de initiatiefnemer hem ziet.
--   toelichting  content:toelichting; vaak meerdere alinea's met opsommingen.
--   opties       de antwoordmogelijkheden bij een list-vraag, op sequenceId.
--   optie_type   enkelAntwoord | meerdereAntwoorden
--   prioriteit   inter:prioriteit — de volgorde waarin het Omgevingsloket
--                vraagt. Zonder dit veld is een vragenlijst een willekeurige
--                verzameling.
--
-- IDEMPOTENT.
-- Draaien: psql "$DB_URL" -v ON_ERROR_STOP=1 -f 2026-09-add-uitvoeringsregel-vraagtekst.sql

BEGIN;

ALTER TABLE i2a.uitvoeringsregel
    ADD COLUMN IF NOT EXISTS label       text,
    ADD COLUMN IF NOT EXISTS toelichting text,
    ADD COLUMN IF NOT EXISTS opties      text[],
    ADD COLUMN IF NOT EXISTS optie_type  text,
    ADD COLUMN IF NOT EXISTS prioriteit  integer;

COMMENT ON COLUMN i2a.uitvoeringsregel.label IS
    'De regel zoals de initiatiefnemer hem ziet: uitv:vraagTekst bij een Vraag, '
    'uitv:bijlageType bij een Bijlage. Eén kolom omdat een vragenlijst één tekst '
    'per rij wil; regel_type zegt waar hij vandaan komt.';
COMMENT ON COLUMN i2a.uitvoeringsregel.toelichting IS
    'content:toelichting. Let op: de bron escapet leestekens (\. \- \*) als '
    'markdown; de parser maakt dat ongedaan voor weergave.';
COMMENT ON COLUMN i2a.uitvoeringsregel.prioriteit IS
    'inter:prioriteit — de volgorde waarin het Omgevingsloket de vraag stelt.';

CREATE INDEX IF NOT EXISTS idx_uitv_label_trgm
    ON i2a.uitvoeringsregel USING gin (lower(label) gin_trgm_ops)
    WHERE label IS NOT NULL;

COMMIT;
