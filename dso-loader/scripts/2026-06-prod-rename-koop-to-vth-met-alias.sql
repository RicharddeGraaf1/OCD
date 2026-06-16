-- =============================================================================
-- PROD: hernoem schema koop -> vth, mét tijdelijke koop-view-alias
-- =============================================================================
-- Uitgevoerd op de productie-Railway-DB (postgis-17) op 2026-06-08.
--
-- Context: de prod-DB had nog `koop` (de dev-rename van 2026-06-03 raakte alleen
-- de lokale dev-DB). De live API draait nog code van 1 juni die `koop.*` leest;
-- die deploy zit vast (GitHub-auto-deploy triggert niet). Een kale rename zou de
-- vergunningen-viewer breken.
--
-- Daarom: rename + een `koop`-schema met read-only views naar `vth`. Zo werkt
-- de oude (koop-)code én de nieuwe (vth-)code. De API is op prod read-only
-- (alleen SELECT; de loader schrijft naar de dev-DB), dus views volstaan.
-- De alias kan weg zodra de vth-code is gedeployed.
--
-- Atomisch + idempotent.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('koop.vergunningkennisgeving') IS NOT NULL
       AND to_regclass('vth.vergunningkennisgeving') IS NULL THEN
        EXECUTE 'ALTER SCHEMA koop RENAME TO vth';
        RAISE NOTICE 'Schema koop -> vth hernoemd.';
    ELSIF to_regclass('vth.vergunningkennisgeving') IS NOT NULL THEN
        RAISE NOTICE 'vth bestaat al — rename overgeslagen.';
    ELSE
        RAISE EXCEPTION 'Noch koop noch vth gevonden — gestopt (handmatig checken).';
    END IF;
END $$;

-- koop-alias: schema + read-only views naar vth.* (voor de nog-niet-geredeployde code)
CREATE SCHEMA IF NOT EXISTS koop;
CREATE OR REPLACE VIEW koop.vergunningkennisgeving AS SELECT * FROM vth.vergunningkennisgeving;
CREATE OR REPLACE VIEW koop.vergunning_deeplink    AS SELECT * FROM vth.vergunning_deeplink;
CREATE OR REPLACE VIEW koop.etl_run                AS SELECT * FROM vth.etl_run;

COMMIT;
