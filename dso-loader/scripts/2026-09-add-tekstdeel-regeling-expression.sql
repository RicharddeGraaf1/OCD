-- De regeling waaruit een tekstdeel is geladen.
--
-- Zonder deze kolom is er geen enkele weg van een tekstdeel naar zijn regeling,
-- en dat is precies het gat van vault-G-141: `repliceer_p2p_naar_prod.py` bouwt
-- zijn scope vanaf `p2p.juridische_regel` omlaag, en een omgevingsvisie of
-- programma heeft die niet — hun inhoud hangt aan divisieannotaties. Daardoor
-- bereikte de replicatie hun tekstdelen, gebiedsaanwijzingen en locaties nooit.
-- Gemeten bij de sync van 2026-09-04, één bronhouder (pv28, visie + programma +
-- verordening in één run): 258 locaties, 158 gebiedsaanwijzingen en 349
-- tekstdelen ontbraken op productie, terwijl de replicatie exit 0 gaf.
--
-- `divisie_wid` is hiervoor geen alternatief. Gemeten over alle 27.817 rijen:
-- hij joint 0 keer op `tekst_element.wid`, draagt in 20.410 gevallen dezelfde
-- UUID als `identificatie` en is in 2.707 gevallen leeg.
--
-- Bewust NULLable en zonder FK: net als bij de andere IMOW-objecten mag een
-- expressie verdwijnen zonder het object mee te nemen. NULL betekent hier
-- "herkomst onbekend" — de voorraad van vóór deze migratie, voor zover de
-- ZIP-cache hem niet kan verklaren.

ALTER TABLE p2p.tekstdeel
    ADD COLUMN IF NOT EXISTS regeling_expression TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_tekstdeel_regeling_expression
    ON p2p.tekstdeel (regeling_expression);

COMMENT ON COLUMN p2p.tekstdeel.regeling_expression IS
    'FRBR-expression van de regeling waaruit dit tekstdeel is geladen. '
    'NULL = herkomst onbekend (voorraad van vóór 2026-09-05). Zie vault G-141.';
