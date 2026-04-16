-- =============================================================
-- Migratiescript: dso → core/p2p/wro/i2a/v2a
-- =============================================================
-- Zie OCD/SCHEMA-INDELING.md voor onderbouwing.
--
-- Uitvoering:
--   psql -d <dbname> -f scripts/migrate_to_keten_schemas.sql
--
-- Idempotent per schema-blok (transactioneel). Bij fout: rollback.
-- Data wordt NIET gekopieerd — `ALTER TABLE ... SET SCHEMA` is
-- een metadata-operatie. Indices, FK's en PostGIS-constraints
-- verhuizen automatisch mee.
-- =============================================================

BEGIN;

-- -------------------------------------------------------------
-- Stap 1: Nieuwe schema's aanmaken (idempotent)
-- -------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS p2p;
CREATE SCHEMA IF NOT EXISTS wro;
CREATE SCHEMA IF NOT EXISTS i2a;
CREATE SCHEMA IF NOT EXISTS v2a;

COMMENT ON SCHEMA core IS 'Referentiegegevens: waardelijsten, bronhouder';
COMMENT ON SCHEMA p2p IS 'Plan-tot-publicatie (Ow): STOP-regelingen, besluiten, CIM-OW objecten';
COMMENT ON SCHEMA wro IS 'Oud regime: Wro/IMRO bestemmingsplannen (sunset 2032)';
COMMENT ON SCHEMA i2a IS 'Idee-tot-afhandeling: IMTR toepasbare regels, werkzaamheden';
COMMENT ON SCHEMA v2a IS 'Vraag-tot-antwoord: viewer-data, vergunningen (gereserveerd)';

-- -------------------------------------------------------------
-- Stap 2: core — waardelijsten en bronhouder (16 tabellen)
-- -------------------------------------------------------------
ALTER TABLE IF EXISTS dso.bestemmingshoofdgroep         SET SCHEMA core;
ALTER TABLE IF EXISTS dso.dubbelbestemmingshoofdgroep   SET SCHEMA core;
ALTER TABLE IF EXISTS dso.bouwaanduidingtype            SET SCHEMA core;
ALTER TABLE IF EXISTS dso.maatvoeringsaanduiding        SET SCHEMA core;
ALTER TABLE IF EXISTS dso.figuurtype                    SET SCHEMA core;
ALTER TABLE IF EXISTS dso.gebiedsaanduidinghoofdgroep   SET SCHEMA core;
ALTER TABLE IF EXISTS dso.dossierstatus                 SET SCHEMA core;
ALTER TABLE IF EXISTS dso.planstatus                    SET SCHEMA core;
ALTER TABLE IF EXISTS dso.regelingmodel                 SET SCHEMA core;
ALTER TABLE IF EXISTS dso.besluitmodel                  SET SCHEMA core;
ALTER TABLE IF EXISTS dso.publicatiebladtype            SET SCHEMA core;
ALTER TABLE IF EXISTS dso.idealisatie                   SET SCHEMA core;
ALTER TABLE IF EXISTS dso.toestemmingstype              SET SCHEMA core;
ALTER TABLE IF EXISTS dso.documenttype                  SET SCHEMA core;
ALTER TABLE IF EXISTS dso.waardelijst                   SET SCHEMA core;
ALTER TABLE IF EXISTS dso.bronhouder                    SET SCHEMA core;

-- -------------------------------------------------------------
-- Stap 3: p2p — STOP-regelingen, besluiten, CIM-OW (16 tabellen)
-- -------------------------------------------------------------
ALTER TABLE IF EXISTS dso.regeling                       SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.besluit                        SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.besluit_regeling               SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.procedurestap                  SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.tekst_element                  SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.geo_informatieobject           SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.juridische_borging             SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.locatie                        SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.locatiegroep_lid               SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.juridische_regel               SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.activiteit                     SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.activiteit_locatieaanduiding   SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.gebiedsaanwijzing              SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.juridische_regel_gebiedsaanwijzing SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.norm                           SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.normwaarde                     SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.juridische_regel_norm          SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.tekstdeel                      SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.hoofdlijn                      SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.tekstdeel_hoofdlijn            SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.pons                           SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.kaart                          SET SCHEMA p2p;
ALTER TABLE IF EXISTS dso.kaartlaag                      SET SCHEMA p2p;

-- -------------------------------------------------------------
-- Stap 4: wro — oud regime (7 tabellen)
-- -------------------------------------------------------------
ALTER TABLE IF EXISTS dso.wro_manifest                   SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.wro_dossier                    SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.ruimtelijk_instrument          SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.planobject                     SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.wro_tekst_object               SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.wro_geleideformulier           SET SCHEMA wro;
ALTER TABLE IF EXISTS dso.wro_bronbestand                SET SCHEMA wro;

-- -------------------------------------------------------------
-- Stap 5: i2a — IMTR toepasbare regels (7 tabellen)
-- -------------------------------------------------------------
ALTER TABLE IF EXISTS dso.regelbeheerobject              SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.toepasbaar_regelbestand        SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.dmn_element                    SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.uitvoeringsregel               SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.werkzaamheid                   SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.aansluitpunt                   SET SCHEMA i2a;
ALTER TABLE IF EXISTS dso.aansluiting                    SET SCHEMA i2a;

-- -------------------------------------------------------------
-- Stap 6: Oude dso-schema droppen
-- -------------------------------------------------------------
-- Na de ALTERs is dso leeg. DROP SCHEMA dso is daarmee veilig.
-- Als er onverwacht nog tabellen in staan, faalt deze stap en
-- blijft de transactie intact zodat we kunnen onderzoeken.
DROP SCHEMA IF EXISTS dso RESTRICT;

-- -------------------------------------------------------------
-- Stap 7: search_path default voor handmatige sessies
-- -------------------------------------------------------------
-- Zodat `SELECT * FROM regeling` in psql/pgAdmin werkt zonder
-- elke keer 'p2p.' ervoor te typen. Applicatiecode gebruikt
-- overal expliciete prefixen, dus dit raakt alleen handmatig werk.
ALTER ROLE postgres IN DATABASE dso
    SET search_path = p2p, wro, i2a, v2a, core, public;

-- -------------------------------------------------------------
-- Stap 8: Planner-statistieken vernieuwen
-- -------------------------------------------------------------
ANALYZE core.bronhouder;
ANALYZE p2p.regeling;
ANALYZE p2p.tekst_element;
ANALYZE p2p.juridische_regel;
ANALYZE p2p.activiteit;
ANALYZE p2p.activiteit_locatieaanduiding;
ANALYZE p2p.locatie;
ANALYZE wro.ruimtelijk_instrument;
ANALYZE wro.planobject;
ANALYZE wro.wro_tekst_object;
ANALYZE i2a.toepasbaar_regelbestand;
ANALYZE i2a.dmn_element;
ANALYZE i2a.werkzaamheid;

COMMIT;

-- -------------------------------------------------------------
-- Verificatie (los, geen transactie nodig)
-- -------------------------------------------------------------
-- Controleer aantallen per schema:
--   SELECT table_schema, count(*) FROM information_schema.tables
--   WHERE table_schema IN ('core','p2p','wro','i2a','v2a')
--   GROUP BY table_schema ORDER BY table_schema;
--
-- Verwacht: core=16, p2p=23, wro=7, i2a=7, v2a=0
