-- ============================================================================
-- 2026-05 · Naam-match signaal (drieslag tekst↔object — type 3)
--
-- Materialized view die per tekst_element voorkomens van object-namen
-- (Activiteit, Gebiedsaanwijzing, Omgevingsnorm, Omgevingswaarde)
-- detecteert via exact word-boundary match op `inhoud_plain`.
--
-- Algoritmiek (gebruiker-keuze 2026-05-08):
--   - Exact match, case-insensitive (~* met \m...\M boundaries)
--   - Geen fuzzy / Levenshtein / lemma-tisering
--   - Minimumlengte 5 tekens (filter generieke namen)
--
-- Refresh: na elke loader-run via
--   REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal;
--
-- Achtergrond: stap 2 in
--   vault_v1/analysis/Plan implementatie drieslag tekst-object.md
--
-- VEREIST: 2026-05-add-trgm-index.sql moet eerst gedraaid zijn (pg_trgm
-- GIN-index op p2p.tekst_element.inhoud_plain). Zonder die index doet
-- Postgres een Cartesisch product en is de matview-CREATE niet
-- uitvoerbaar (zie [[gaps]] G-68).
--
-- Idempotent: DROP IF EXISTS + CREATE. Re-run is OK; refresh elders.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-naammatch-signaal.sql
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS p2p.naammatch_signaal CASCADE;

-- Belangrijke design-keuze (gebruiker, 2026-05-08): de matview match
-- alleen INTRA-regeling. Inter-regeling-matches (een tekst van gemeente A
-- die de naam van een Activiteit van gemeente B bevat) zijn semantisch
-- niet zinvol voor de drieslag-consistentie-check en zorgden in v1 voor
-- 5,1M rijen (factor ~100 explosie). Daarom joint elke object-CTE
-- óók de tekst_element.regeling_expression van die regeling waarin het
-- object via de juridische-regel-laag gebruikt wordt.

CREATE MATERIALIZED VIEW p2p.naammatch_signaal AS
WITH
-- Per Gebiedsaanwijzing: in welke regeling(en) wordt hij geannoteerd?
ga_in_regeling AS (
    SELECT DISTINCT ga.identificatie AS object_id, ga.naam, te.regeling_expression
    FROM p2p.gebiedsaanwijzing ga
    JOIN p2p.juridische_regel_gebiedsaanwijzing jrg
         ON jrg.gebiedsaanwijzing_id = ga.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = jrg.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
    WHERE length(ga.naam) >= 5
),
-- Per Activiteit: via ALA → juridische_regel → regeltekst → regeling
act_in_regeling AS (
    SELECT DISTINCT a.identificatie AS object_id, a.naam, te.regeling_expression
    FROM p2p.activiteit a
    JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
    WHERE length(a.naam) >= 5
),
-- Per Norm (Omgevingsnorm of Omgevingswaarde): via juridische_regel_norm
norm_in_regeling AS (
    SELECT DISTINCT n.identificatie AS object_id, n.naam, n.norm_type, te.regeling_expression
    FROM p2p.norm n
    JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
    WHERE length(n.naam) >= 5
),
naam_kandidaten AS (
    SELECT object_id, naam, 'Gebiedsaanwijzing' AS object_type, regeling_expression
    FROM ga_in_regeling
  UNION ALL
    SELECT object_id, naam, 'Activiteit', regeling_expression
    FROM act_in_regeling
  UNION ALL
    SELECT object_id, naam,
           CASE norm_type WHEN 'Omgevingswaarde' THEN 'Omgevingswaarde'
                          ELSE 'Omgevingsnorm' END,
           regeling_expression
    FROM norm_in_regeling
)
SELECT
    te.id           AS tekst_element_id,
    nk.object_id    AS object_id,
    nk.object_type  AS object_type,
    nk.naam         AS gematchte_naam,
    te.regeling_expression
FROM p2p.tekst_element te
JOIN naam_kandidaten nk
  -- INTRA-regeling: alleen matches binnen dezelfde regeling
  ON te.regeling_expression = nk.regeling_expression
 AND te.inhoud_plain IS NOT NULL
 -- ILIKE prefilter — gebruikt de pg_trgm GIN-index op inhoud_plain;
 -- zonder deze pre-filter doet Postgres een Cartesisch product en
 -- timeoutet de CREATE.
 AND te.inhoud_plain ILIKE '%' || nk.naam || '%'
 -- Refinement: exact word-boundary match (\m...\M) voor exact-match-eis.
 AND te.inhoud_plain ~* (
     '\m' ||
     regexp_replace(nk.naam, '([\.\^\$\*\+\?\(\)\[\]\{\}\\\|])', '\\\1', 'g') ||
     '\M'
 );

-- UNIQUE index is verplicht voor REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX naammatch_signaal_pk
    ON p2p.naammatch_signaal (tekst_element_id, object_type, object_id);

CREATE INDEX naammatch_signaal_object
    ON p2p.naammatch_signaal (object_type, object_id);

CREATE INDEX naammatch_signaal_te
    ON p2p.naammatch_signaal (tekst_element_id);

CREATE INDEX naammatch_signaal_regeling
    ON p2p.naammatch_signaal (regeling_expression);

COMMENT ON MATERIALIZED VIEW p2p.naammatch_signaal IS
    'Naam-overeenkomsten tussen object-namen (Activiteit/GA/Norm/Omgevingswaarde) '
    'en tekst_element.inhoud_plain BINNEN DEZELFDE REGELING. Exact match, '
    'case-insensitive, word-boundary, minimumlengte 5. Type 3 in drieslag '
    'tekst↔object. Heuristiek — niet samenvoegen met type 1 (annotatie) of type 2 '
    '(IntIoRef) zonder expliciete provenance. Inter-regeling-matches zijn bewust '
    'uitgesloten (zelfde naam in 100+ gemeenten = false-positive-explosie, gebruiker-keuze 2026-05-08).';
