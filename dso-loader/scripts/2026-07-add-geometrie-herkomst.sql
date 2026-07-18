-- ============================================================================
-- 2026-07 · wro.ruimtelijk_instrument.geometrie_herkomst
--
-- Onderscheidt precieze plangebied-geometrie (PDOK-GML) van een indicatieve
-- ambtsgebied-placeholder voor oude IMRO2006/Artikel-10-plannen die geen
-- machine-leesbare geometrie hebben (zie loaders/wro_imro2006.py). Zo kan de
-- viewer/bot ze apart tonen ("gemeentebreed, exacte begrenzing onbekend").
--
-- Waarden: NULL / 'pdok-gml' = precies · 'ambtsgebied-imro2006' = indicatief.
-- Idempotent.
-- ============================================================================

ALTER TABLE wro.ruimtelijk_instrument
    ADD COLUMN IF NOT EXISTS geometrie_herkomst TEXT NULL;
