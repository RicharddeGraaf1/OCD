-- ============================================================================
-- 2026-05 · tekst_object_consistentie v3 — koppeling via basisgeo-junctions
--
-- Werkblok 4 herzien (2026-05-12). v2 koppelde via
-- `geo_informatieobject.geometrie_identificatie ↔ locatie.geometrie_identificatie`
-- maar die kolommen zaten in twee verschillende UUID-stelsels (Presenteren
-- vs. GML), zie [[gaps]] G-69 toelichting.
--
-- v3 gebruikt M:N junction-tabellen p2p.locatie_basisgeo en
-- p2p.gio_basisgeo (gevuld via dso-loader/src/loaders/gio_zip.py):
--
--   IntIoRef.target_gio_expression
--     → geo_informatieobject.frbr_expression
--     → gio_basisgeo.basisgeo_id          [M:N]
--     ↔ locatie_basisgeo.basisgeo_id      [M:N]
--     → locatie.identificatie
--     → gebiedsaanwijzing.locatie_id      (of normwaarde.locatie_id)
--     → object_id
--
-- doelen_match: vergelijkt de basisgeo:ids van type 1 (via object →
-- locatie → locatie_basisgeo) met die van type 2 (via IntIoRef → GIO →
-- gio_basisgeo). Tegenstrijdig als ze elkaar niet overlappen.
--
-- Idempotent: CREATE OR REPLACE VIEW.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-object-consistentie-v3.sql
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
    -- Route 2: IntIoRef → GIO → gio_basisgeo ↔ locatie_basisgeo → locatie → GA
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
            -- Type 2 via junctions
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
        -- doelen_match: overlap basisgeo:ids tussen object-locatie en intioref-gios
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
    tekst_element_id,
    object_type,
    object_id,
    has_annotatie,
    has_intioref,
    has_naammatch,
    doelen_match,
    CASE
        WHEN has_annotatie AND COALESCE(has_intioref, FALSE) AND doelen_match = FALSE
            THEN 'tegenstrijdige_doelen'
        WHEN has_annotatie
             AND COALESCE(has_intioref, TRUE)
             AND has_naammatch
            THEN 'consistent_aanwezig'
        WHEN NOT has_annotatie
             AND (COALESCE(has_intioref, FALSE) OR has_naammatch)
            THEN 'vermoedelijk_vergeten_annotatie'
        WHEN has_annotatie
             AND NOT COALESCE(has_intioref, FALSE)
             AND NOT has_naammatch
            THEN 'annotatie_zonder_naam_match'
        WHEN has_annotatie
            THEN 'consistent_aanwezig'
        ELSE 'overig_inconsistent'
    END AS consistentie_klasse
FROM alle_kandidaten;

COMMENT ON VIEW p2p.tekst_object_consistentie IS
    'v3 (2026-05-12) — type-2-keten en doelen_match lopen via M:N junction-'
    'tabellen p2p.locatie_basisgeo en p2p.gio_basisgeo (gevuld vanuit '
    'OW-bestanden in de Download-ZIP). Vervangt v2-koppeling via '
    'geometrie_identificatie-kolommen, die in twee verschillende UUID-stelsels '
    'zaten.';
