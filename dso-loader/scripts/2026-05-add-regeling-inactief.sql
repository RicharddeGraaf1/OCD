-- Markeer regelingen die lokaal staan maar door DSO niet meer als aangeleverd
-- worden gemeld (bv. ingetrokken, vervangen, of bronhouder-code gewijzigd).
-- Detectie via scripts/diff_dso_bronhouder_coverage.py; markering hier.
--
-- `inactief` = booleaanse vlag; queries die alleen vigerende regelingen willen
-- tonen kunnen `WHERE NOT inactief` toevoegen. `datum_inactief` legt vast
-- wanneer wij de regeling als inactief markeerden (≠ moment van intrekken
-- door bevoegd gezag — dat is via STOP-besluiten niet gemakkelijk te bepalen).

ALTER TABLE p2p.regeling
    ADD COLUMN IF NOT EXISTS inactief       BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS datum_inactief TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_regeling_inactief
    ON p2p.regeling(inactief) WHERE inactief = TRUE;
