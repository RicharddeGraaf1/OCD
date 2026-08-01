-- ══════════════════════════════════════════════════════════════════
-- Backfill: bestaande p2pwijziging.besluit-rijen hebben
-- wijzigt_expression == nieuwe_expression (loader-bug: beide velden
-- kregen `expression_id`). De echte basis-expression zat alleen in
-- _links.beoogdeOpvolgerVan / _links.wijzigtRegelingversie en werd
-- niet ge-extraheerd.
--
-- Vanaf commit-datum haalt ontwerp_loader._fetch_basis_expression de
-- basis wél op (extra API-call per ontwerp). Voor bestaande rijen:
-- zet wijzigt_expression op NULL zodat:
--   1. de viewer-A1-filter ze verbergt (NULL matcht niets in p2p),
--   2. een reguliere re-ingest ze via ON CONFLICT DO UPDATE bijwerkt
--      met de echte basis.
--
-- Vervang-regelingen (is_vervang_regeling=TRUE) worden overgeslagen —
-- die hebben conceptueel geen basis en zijn sowieso al gefilterd door
-- de viewer-query.
-- ══════════════════════════════════════════════════════════════════

BEGIN;

UPDATE p2pwijziging.besluit
SET    wijzigt_expression = NULL
WHERE  is_vervang_regeling = FALSE
  AND  wijzigt_expression = nieuwe_expression;

-- Sanity: alle niet-vervang-rijen zouden nu NULL moeten hebben
-- (of iets anders dan nieuwe_expression, mocht een eerdere handmatige
-- update dat al gezet hebben).
SELECT COUNT(*) AS totaal,
       COUNT(*) FILTER (WHERE wijzigt_expression IS NULL) AS null_basis,
       COUNT(*) FILTER (WHERE wijzigt_expression = nieuwe_expression) AS blijvend_gelijk
FROM   p2pwijziging.besluit
WHERE  is_vervang_regeling = FALSE;

COMMIT;
