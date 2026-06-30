-- Migratie: RP-planvoorraad snapshot-tabellen in het wro-schema.
-- Idempotent (IF NOT EXISTS). Toepasbaar op bestaande DB's zonder de volledige
-- DDL te herdraaien. Zie src/ddl.py (canonieke definitie) + vault
-- analysis/RP-planvoorraad.md.
--
-- NB: de prod-DB is een selective restore; het wro-schema bestaat daar mogelijk
-- nog niet. Daarom expliciet aangemaakt. De twee tabellen zijn zelfstandig
-- (geen FK's naar andere wro/core-tabellen), dus ze werken ook als de rest van
-- het wro-schema niet op prod staat.

CREATE SCHEMA IF NOT EXISTS wro;

CREATE TABLE IF NOT EXISTS wro.wro_snapshot (
    snapshot_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    datum           DATE NOT NULL,
    bron            TEXT NOT NULL DEFAULT 'rp-opvragen-v4',
    aantal_plannen  INT NULL,
    aangemaakt_op   TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (datum, bron)
);

CREATE TABLE IF NOT EXISTS wro.wro_plan_observatie (
    snapshot_id             BIGINT NOT NULL REFERENCES wro.wro_snapshot(snapshot_id) ON DELETE CASCADE,
    identificatie           TEXT NOT NULL,
    dossier                 TEXT NULL,
    bronhouder_code         TEXT NULL,
    bronhouder_naam         TEXT NULL,
    titel                   TEXT NULL,
    plantype                TEXT NULL,
    planstatus              TEXT NULL,
    planstatus_datum        DATE NULL,
    dossierstatus           TEXT NULL,
    is_tam                  BOOLEAN NOT NULL DEFAULT FALSE,
    is_paraplu              BOOLEAN NOT NULL DEFAULT FALSE,
    verwijderd_op           TIMESTAMPTZ NULL,
    einde_rechtsgeldigheid  TEXT NULL,
    relaties                JSONB NULL,
    PRIMARY KEY (snapshot_id, identificatie)
);
CREATE INDEX IF NOT EXISTS idx_wro_planobs_ident ON wro.wro_plan_observatie(identificatie);
CREATE INDEX IF NOT EXISTS idx_wro_planobs_bronhouder ON wro.wro_plan_observatie(bronhouder_code);
CREATE INDEX IF NOT EXISTS idx_wro_planobs_verwijderd ON wro.wro_plan_observatie(verwijderd_op);
