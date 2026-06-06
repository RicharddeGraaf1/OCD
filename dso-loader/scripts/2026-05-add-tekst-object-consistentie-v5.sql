-- ============================================================================
-- 2026-05 · tekst_object_consistentie v5 — naam-match als harde eis voor
--                                          'vermoedelijk_vergeten_annotatie';
--                                          nieuwe klasse voor IntIoRef-alleen.
--
-- v4 (zie 2026-05-add-tekst-object-consistentie-v4.sql) classificeerde een
-- (tekst, object)-paar als `vermoedelijk_vergeten_annotatie` zodra
-- `has_intioref` OF `has_naammatch` TRUE was, en `has_annotatie` FALSE.
--
-- Empirische verificatie (2026-05-19, geval 3 in de
-- "drie voorbeelden van vergeten annotaties" query) wees uit dat
-- `has_intioref=TRUE` géén unieke aanwijzing van het object levert
-- zodra meerdere objecten (typisch Bouwlaag-Wonen, Bouwlaag-Dienstverlening,
-- Bouwlaag-Kantoor, Bouwlaag-Bedrijf-functiemenging) gekoppeld zijn aan
-- dezelfde locatie/basisgeo-set. Eén IntIoRef wijst dan naar de gedeelde
-- geometrie en de keten levert alle gestapelde objecten als kandidaat
-- terug — niet alleen het object dat de tekst feitelijk noemt.
--
-- Schaal van het probleem (2026-05-19):
--   • Omgevingsnorm:    94% van 'vermoedelijk_vergeten' kwam alleen uit IntIoRef
--   • Gebiedsaanwijzing: 68% van 'vermoedelijk_vergeten' kwam alleen uit IntIoRef
--   • 18% van normen / 13% van GA's deelt zijn basisgeo-set met >=1 ander
--     object van hetzelfde type binnen dezelfde bronhouder
--
-- v5-aanpassing (gebruiker-keuze 2026-05-19, optie B):
--   • `vermoedelijk_vergeten_annotatie` alleen als `has_naammatch = TRUE`.
--     IntIoRef alleen is onvoldoende bewijs.
--   • Nieuwe klasse `intioref_zonder_annotatie_of_naam` vangt het
--     gedegradeerde signaal op: er is wél een IntIoRef-keten die dit
--     object aanwijst, maar zonder annotatie en zonder naam-match. Mogelijk
--     irrelevant (gestapeld object) of zwakker signaal dan vergeten.
--
-- Idempotent: CREATE OR REPLACE VIEW.
-- Vereist: 2026-05-add-niet-annoteerbaar.sql gedraaid.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-object-consistentie-v5.sql
--       Vervolgens: REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.tekst_object_consistentie_mv;
-- ============================================================================

CREATE OR REPLACE VIEW p2p.tekst_object_consistentie AS

WITH ga_kandidaten AS (
    SELECT te.id AS tekst_element_id, ga.identificatie AS object_id
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
    JOIN p2p.juridische_regel_gebiedsaanwijzing jrg
         ON jrg.juridische_regel_id = jr.identificatie
    JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jrg.gebiedsaanwijzing_id
  UNION
    SELECT tir.tekst_element_id, ga.identificatie
    FROM p2p.tekst_inline_referentie tir
    JOIN p2p.gio_basisgeo gb ON gb.gio_frbr = tir.target_gio_expression
    JOIN p2p.locatie_basisgeo lb ON lb.basisgeo_id = gb.basisgeo_id
    JOIN p2p.gebiedsaanwijzing ga ON ga.locatie_id = lb.locatie_id
    WHERE tir.soort = 'IntIoRef'
  UNION
    SELECT ns.tekst_element_id, ns.object_id
    FROM p2p.naammatch_signaal_intra ns
    WHERE ns.object_type = 'Gebiedsaanwijzing'
),
ga_evaluated AS (
    SELECT
        k.tekst_element_id,
        'Gebiedsaanwijzing'::TEXT AS object_type,
        k.object_id,
        EXISTS (
            SELECT 1
            FROM p2p.tekst_element te2
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
            JOIN p2p.juridische_regel_gebiedsaanwijzing jrg
                 ON jrg.juridische_regel_id = jr.identificatie
            WHERE te2.id = k.tekst_element_id
              AND jrg.gebiedsaanwijzing_id = k.object_id
        ) AS has_annotatie,
        EXISTS (
            SELECT 1
            FROM p2p.tekst_inline_referentie tir
            JOIN p2p.gio_basisgeo gb ON gb.gio_frbr = tir.target_gio_expression
            JOIN p2p.locatie_basisgeo lb ON lb.basisgeo_id = gb.basisgeo_id
            JOIN p2p.gebiedsaanwijzing ga2 ON ga2.locatie_id = lb.locatie_id
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
              AND ga2.identificatie = k.object_id
        ) AS has_intioref,
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_type = 'Gebiedsaanwijzing'
              AND ns.object_id = k.object_id
        ) AS has_naammatch,
        (
            SELECT bool_and(EXISTS (
                SELECT 1
                FROM p2p.locatie_basisgeo lb_obj
                WHERE lb_obj.locatie_id = ga_match.locatie_id
                  AND lb_obj.basisgeo_id IN (
                      SELECT gb.basisgeo_id
                      FROM p2p.gio_basisgeo gb
                      WHERE gb.gio_frbr = tir.target_gio_expression
                  )
            ))
            FROM p2p.gebiedsaanwijzing ga_match
            JOIN p2p.tekst_inline_referentie tir
              ON tir.tekst_element_id = k.tekst_element_id
             AND tir.soort = 'IntIoRef'
             AND tir.target_gio_expression IS NOT NULL
            WHERE ga_match.identificatie = k.object_id
        ) AS doelen_match
    FROM ga_kandidaten k
),

act_kandidaten AS (
    SELECT te.id AS tekst_element_id, a.identificatie AS object_id
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
    JOIN p2p.activiteit_locatieaanduiding ala
         ON ala.juridische_regel_id = jr.identificatie
    JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
  UNION
    SELECT ns.tekst_element_id, ns.object_id
    FROM p2p.naammatch_signaal_intra ns
    WHERE ns.object_type = 'Activiteit'
),
act_evaluated AS (
    SELECT
        k.tekst_element_id,
        'Activiteit'::TEXT AS object_type,
        k.object_id,
        EXISTS (
            SELECT 1
            FROM p2p.tekst_element te2
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
            JOIN p2p.activiteit_locatieaanduiding ala
                 ON ala.juridische_regel_id = jr.identificatie
            WHERE te2.id = k.tekst_element_id
              AND ala.activiteit_id = k.object_id
        ) AS has_annotatie,
        NULL::BOOLEAN AS has_intioref,
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_type = 'Activiteit'
              AND ns.object_id = k.object_id
        ) AS has_naammatch,
        NULL::BOOLEAN AS doelen_match
    FROM act_kandidaten k
),

norm_kandidaten AS (
    SELECT te.id AS tekst_element_id, n.identificatie AS object_id, n.norm_type
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
    JOIN p2p.juridische_regel_norm jrn
         ON jrn.juridische_regel_id = jr.identificatie
    JOIN p2p.norm n ON n.identificatie = jrn.norm_id
  UNION
    SELECT tir.tekst_element_id, n.identificatie, n.norm_type
    FROM p2p.tekst_inline_referentie tir
    JOIN p2p.gio_basisgeo gb ON gb.gio_frbr = tir.target_gio_expression
    JOIN p2p.locatie_basisgeo lb ON lb.basisgeo_id = gb.basisgeo_id
    JOIN p2p.normwaarde nw ON nw.locatie_id = lb.locatie_id
    JOIN p2p.norm n ON n.identificatie = nw.norm_id
    WHERE tir.soort = 'IntIoRef'
  UNION
    SELECT ns.tekst_element_id, ns.object_id,
           CASE ns.object_type WHEN 'Omgevingswaarde' THEN 'Omgevingswaarde' ELSE 'Omgevingsnorm' END
    FROM p2p.naammatch_signaal_intra ns
    WHERE ns.object_type IN ('Omgevingsnorm', 'Omgevingswaarde')
),
norm_evaluated AS (
    SELECT
        k.tekst_element_id,
        CASE k.norm_type WHEN 'Omgevingswaarde' THEN 'Omgevingswaarde' ELSE 'Omgevingsnorm' END AS object_type,
        k.object_id,
        EXISTS (
            SELECT 1
            FROM p2p.tekst_element te2
            JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te2.wid
            JOIN p2p.juridische_regel_norm jrn
                 ON jrn.juridische_regel_id = jr.identificatie
            WHERE te2.id = k.tekst_element_id
              AND jrn.norm_id = k.object_id
        ) AS has_annotatie,
        EXISTS (
            SELECT 1
            FROM p2p.tekst_inline_referentie tir
            JOIN p2p.gio_basisgeo gb ON gb.gio_frbr = tir.target_gio_expression
            JOIN p2p.locatie_basisgeo lb ON lb.basisgeo_id = gb.basisgeo_id
            JOIN p2p.normwaarde nw ON nw.locatie_id = lb.locatie_id
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
              AND nw.norm_id = k.object_id
        ) AS has_intioref,
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_id = k.object_id
              AND ns.object_type IN ('Omgevingsnorm', 'Omgevingswaarde')
        ) AS has_naammatch,
        (
            SELECT bool_and(EXISTS (
                SELECT 1
                FROM p2p.normwaarde nw2
                JOIN p2p.locatie_basisgeo lb_obj ON lb_obj.locatie_id = nw2.locatie_id
                WHERE nw2.norm_id = k.object_id
                  AND lb_obj.basisgeo_id IN (
                      SELECT gb.basisgeo_id FROM p2p.gio_basisgeo gb
                      WHERE gb.gio_frbr = tir.target_gio_expression
                  )
            ))
            FROM p2p.tekst_inline_referentie tir
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
              AND tir.target_gio_expression IS NOT NULL
        ) AS doelen_match
    FROM norm_kandidaten k
),

alle_kandidaten AS (
    SELECT * FROM ga_evaluated
    UNION ALL
    SELECT * FROM act_evaluated
    UNION ALL
    SELECT * FROM norm_evaluated
)

SELECT
    ak.tekst_element_id,
    ak.object_type,
    ak.object_id,
    ak.has_annotatie,
    ak.has_intioref,
    ak.has_naammatch,
    ak.doelen_match,
    CASE
        WHEN te.is_niet_annoteerbaar
            THEN 'niet_annoteerbaar'
        WHEN ak.has_annotatie AND COALESCE(ak.has_intioref, FALSE) AND ak.doelen_match = FALSE
            THEN 'tegenstrijdige_doelen'
        WHEN ak.has_annotatie
             AND COALESCE(ak.has_intioref, TRUE)
             AND ak.has_naammatch
            THEN 'consistent_aanwezig'
        -- v5: 'vermoedelijk_vergeten_annotatie' eist nu een naam-match. IntIoRef
        -- alleen is onvoldoende omdat de IntIoRef→locatie→object-keten niet
        -- discrimineert tussen objecten die op dezelfde locatie gestapeld zijn.
        WHEN NOT ak.has_annotatie
             AND ak.has_naammatch
            THEN 'vermoedelijk_vergeten_annotatie'
        -- v5: nieuw — IntIoRef-keten wijst aan dat dit object kandidaat is
        -- voor deze tekst, maar er is geen annotatie EN geen naam-match.
        -- Vaak: gestapeld object dat de tekst niet expliciet noemt. Zwakker
        -- signaal dan vergeten; los tonen in OCD-viewer.
        WHEN NOT ak.has_annotatie
             AND COALESCE(ak.has_intioref, FALSE)
            THEN 'intioref_zonder_annotatie_of_naam'
        WHEN ak.has_annotatie
             AND NOT COALESCE(ak.has_intioref, FALSE)
             AND NOT ak.has_naammatch
            THEN 'annotatie_zonder_naam_match'
        WHEN ak.has_annotatie
            THEN 'consistent_aanwezig'
        ELSE 'overig_inconsistent'
    END AS consistentie_klasse
FROM alle_kandidaten ak
JOIN p2p.tekst_element te ON te.id = ak.tekst_element_id;

COMMENT ON VIEW p2p.tekst_object_consistentie IS
    'v5 (2026-05-19) — `vermoedelijk_vergeten_annotatie` eist nu has_naammatch=TRUE. '
    'IntIoRef-alleen-zonder-annotatie wordt gerouteerd naar nieuwe klasse '
    '`intioref_zonder_annotatie_of_naam` omdat de keten niet onderscheidt tussen '
    'objecten die dezelfde locatie/basisgeo-set delen (bv. Bouwlaag-Wonen vs '
    'Bouwlaag-Dienstverlening op één transformatiegebied).';
