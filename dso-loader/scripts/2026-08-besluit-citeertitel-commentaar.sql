-- Semantiek van de twee titel-kolommen op p2pwijziging.besluit vastleggen.
--
-- Aanleiding: de loader vulde `citeertitel` met het top-level `citeerTitel`
-- uit de Presenteren-API, en dat is de citeertitel van de REGELING. Daardoor
-- droegen de drie Putten-ontwerpen alle drie "Omgevingsplan gemeente Putten"
-- en waren ze in de viewer niet uit elkaar te houden. De besluit-eigen naam
-- zit in `besluitMetadata.citeerTitel`, dat alleen op het Ontwerpregeling-
-- schema bestaat (0 van 2812 besluitversies levert het, gemeten 2026-08-10).
--
-- Idempotent; hoort bij dso-loader/src/ddl.py.

COMMENT ON COLUMN p2pwijziging.besluit.opschrift IS
    'Opschrift van de REGELING die dit besluit wijzigt (bv. "Omgevingsplan gemeente Putten") — niet onderscheidend tussen besluiten op dezelfde regeling.';

COMMENT ON COLUMN p2pwijziging.besluit.citeertitel IS
    'Citeertitel van het BESLUIT zelf, uit besluitMetadata.citeerTitel (bv. "Wijziging omgevingsplan gemeente Putten t.b.v. ontwikkeling Stenenkamerseweg 38/38a"). Valt terug op de citeertitel van de regeling wanneer de bron geen besluitMetadata levert — dat is bij alle besluitversies zo, want het veld bestaat alleen op ontwerpregelingen.';
