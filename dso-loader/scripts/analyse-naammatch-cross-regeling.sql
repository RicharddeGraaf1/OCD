-- ============================================================================
-- Landelijke naam-kruisvergelijking (on-demand) — voorheen p2p.naammatch_signaal v1
--
-- Tot 2026-08-01 stond deze vergelijking permanent gematerialiseerd: élke tekst
-- in Nederland tegen élke objectnaam in Nederland, 6.324.956 rijen. De laag
-- erboven (naammatch_signaal_intra) gooide daar 99,3% van weg en hield alleen de
-- treffers binnen dezelfde regeling over (43.045). De kruisvergelijking kostte
-- ~68 min per sync en liet de prod-refresh de 3-uurs-timeout overschrijden.
--
-- Ze is niet weggegooid maar on-demand gemaakt: niets in de codebase las haar
-- (geverifieerd 2026-08-01 — geen enkele API-, viewer- of rapportagequery).
-- Draai dit script als je een cross-regeling-vraag hebt, bijvoorbeeld:
--   * welke bronhouders gebruiken dezelfde objectnamen?
--   * noemt een gemeente een begrip dat elders wél een object is en hier niet?
--
-- LET OP: dit is zwaar (~1 uur, 6M+ rijen). Scope het waar mogelijk, bijvoorbeeld
-- met de WHERE-clausule onderaan op één bronhouder of één regeling.
--
-- Vereist de pg_trgm GIN-index op p2p.tekst_element.inhoud_plain
-- (2026-05-add-trgm-index.sql) — zonder die index wordt het een Cartesisch
-- product en loopt de query niet af.
-- ============================================================================

\timing on

WITH naam_kandidaten AS (
    SELECT ga.identificatie AS object_id, ga.naam, 'Gebiedsaanwijzing' AS object_type
    FROM p2p.gebiedsaanwijzing ga
    WHERE length(ga.naam) >= 5
  UNION ALL
    SELECT a.identificatie, a.naam, 'Activiteit'
    FROM p2p.activiteit a
    WHERE length(a.naam) >= 5
  UNION ALL
    SELECT n.identificatie, n.naam,
           CASE n.norm_type WHEN 'Omgevingswaarde' THEN 'Omgevingswaarde'
                            ELSE 'Omgevingsnorm' END
    FROM p2p.norm n
    WHERE length(n.naam) >= 5
)
SELECT
    te.id                   AS tekst_element_id,
    te.regeling_expression  AS tekst_regeling,
    nk.object_id,
    nk.object_type,
    nk.naam                 AS gematchte_naam
FROM p2p.tekst_element te
JOIN naam_kandidaten nk
  ON te.inhoud_plain IS NOT NULL
 -- ILIKE-prefilter gebruikt de trigram-index; zonder deze regel loopt het niet af.
 AND te.inhoud_plain ILIKE '%' || nk.naam || '%'
 -- Refinement: exacte woordgrens-match. De escaping van haakjes is essentieel —
 -- zonder deze regexp_replace wordt '48 dB(A) geluidscontour' als regex-groep
 -- gelezen en valt de naam stil weg.
 AND te.inhoud_plain ~* (
     '\m' ||
     regexp_replace(nk.naam, '([\.\^\$\*\+\?\(\)\[\]\{\}\\\|])', '\\\1', 'g') ||
     '\M'
 )
-- Scope dit! Bijvoorbeeld:
-- WHERE te.regeling_expression LIKE '/akn/nl/act/gm0344/%'
;
