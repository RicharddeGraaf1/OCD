-- ============================================================================
-- 2026-08 · i2a-delta: onthoud per regelbestand wanneer het laatst wijzigde.
--
-- De i2a-fase kostte na de prefixfix (G-117) 5,6 uur, en dat zit vrijwel
-- volledig in het downloaden en parsen van de DMN-XML: één call per
-- regelbestand, ~150 per bronhouder, 343 bronhouders — ongeveer 50.000
-- downloads per sync.
--
-- De lijst-call (`GET /toepasbareRegels?oin=…`) is daarentegen goedkoop en
-- levert per bestand een `laatsteWijzigingDatum` op secondeniveau. Door die op
-- te slaan kan de volgende run de XML overslaan zolang de datum niet is
-- veranderd. Gemeten op één bronhouder: 166 regelbestanden, allemaal met een
-- wijzigingsdatum uit juli 2023.
--
-- De kolom wordt bewust pas gevuld NA een geslaagde XML-verwerking. Zo betekent
-- "datum aanwezig en gelijk" ook echt "de inhoud van deze versie staat erin",
-- en niet slechts "we hebben de metadata ooit gezien".
--
-- TEXT en geen timestamp: de API levert `dd-mm-yyyy HH:MM:SS`, en een exacte
-- stringvergelijking is hier robuuster dan parsen met tijdzone-aannames.
-- ============================================================================

ALTER TABLE i2a.toepasbaar_regelbestand
    ADD COLUMN IF NOT EXISTS laatste_wijziging TEXT;

COMMENT ON COLUMN i2a.toepasbaar_regelbestand.laatste_wijziging IS
    'laatsteWijzigingDatum uit de STTR-lijst, zoals geleverd (dd-mm-yyyy HH:MM:SS). '
    'Alleen gezet na een geslaagde DMN-verwerking; gelijk = XML-download overslaan.';
