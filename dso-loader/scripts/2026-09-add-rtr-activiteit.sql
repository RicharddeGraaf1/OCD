-- Registratiegegevens per activiteit, zoals de RTR ze publiceert.
--
-- Apart van p2p.activiteit, en dat is de kern van het onderscheid: p2p is de
-- activiteit zoals hij in de omgevingsdocumenten geannoteerd staat (IMOW),
-- dit is de activiteit zoals hij in het register van toepasbare regels
-- geregistreerd staat (RTR). Andere auteur, andere geldigheid, en ze kunnen
-- uiteenlopen -- dat verschil is juist wat een onafhankelijke viewer hoort te
-- laten zien in plaats van glad te strijken.
--
-- Wat de publieke RTR v2 NIET levert, ook niet op het detail-endpoint
-- (gemeten 2026-09-07): `toonbaar` en magneetactiviteit. Die kolommen staan
-- hier dus bewust niet; ze zouden alleen met NULL gevuld kunnen worden.
CREATE TABLE IF NOT EXISTS i2a.rtr_activiteit (
    urn              text PRIMARY KEY,
    omschrijving     text,
    oin              text,
    organisatie_type text,          -- GM | WS | PV | MNRE
    organisatie_code text,
    bestuurslaag     text,
    begin_datum      date,
    eind_datum       date,          -- vrijwel altijd NULL; zie hieronder
    verfijnbaar      boolean,
    locaties         jsonb,         -- [{identificatie, beginDatum, eindDatum}]
    aantal_rbo       int,
    opgehaald_op     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_rtr_act_org
    ON i2a.rtr_activiteit (organisatie_type, organisatie_code);
CREATE INDEX IF NOT EXISTS ix_rtr_act_eind
    ON i2a.rtr_activiteit (eind_datum) WHERE eind_datum IS NOT NULL;

COMMENT ON COLUMN i2a.rtr_activiteit.eind_datum IS
  'Gemeten 2026-09-07: Amsterdam, Den Haag, Noord-Holland en Rijnland zetten '
  'er geen; Utrecht zet op AL zijn activiteiten 17-09-2026. Een gevulde '
  'einddatum is daarom een uitzondering en een signaal, geen routineveld.';
