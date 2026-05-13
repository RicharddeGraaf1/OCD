-- ============================================================================
-- 2026-05 · tekst_object_consistentie v2 — koppeling via geometrie_identificatie
--
-- Werkblok 4 van [[Plan implementatie GIO-laden]] §"Optie C-light".
--
-- Verandering t.o.v. v1 (2026-05-add-tekst-object-consistentie.sql):
-- De type-2-kandidaten-keten voor GA/Norm/Omgevingswaarde liep eerder
-- via `p2p.juridische_borging` (`gio_expression → domein_object_id`),
-- die optioneel is en niet geladen wordt (G-69). v2 vervangt dat door
-- de keten via gedeelde geometrie-UUID:
--
--   tekst_inline_referentie.target_gio_expression
--     → geo_informatieobject.frbr_expression
--     → geo_informatieobject.geometrie_identificatie
--     ↔ locatie.geometrie_identificatie         (gedeelde UUID)
--     → gebiedsaanwijzing.locatie_id            (of normwaarde.locatie_id)
--     → object_id
--
-- Dezelfde keten wordt gebruikt om `doelen_match` te bepalen voor
-- `tegenstrijdige_doelen`: de geometrie-UUID die type 2 noemt moet
-- overeenkomen met de UUID van het object's locatie.
--
-- Voor Activiteit verandert er niets — Activiteit heeft geen IO en
-- `has_intioref` blijft NULL.
--
-- Klasse-mapping ongewijzigd (zie v1).
--
-- Vereist: 2026-05-add-geometrie-identificatie.sql (kolommen) + werkblok 2
-- (Locatie-loader vult locatie.geometrie_identificatie) + werkblok 3
-- (GIO-loader vult geo_informatieobject.geometrie_identificatie).
-- Zonder die data blijft de view leeg voor type-2-kandidaten via deze
-- keten, maar er ontstaan geen fouten — de JOINs filteren op NOT NULL.
--
-- Idempotent: CREATE OR REPLACE VIEW.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-object-consistentie-v2.sql
-- ============================================================================

CREATE OR REPLACE VIEW p2p.tekst_object_consistentie AS

-- Gebiedsaanwijzing — alle drie mechanismen toepasselijk
WITH ga_kandidaten AS (
    -- Route 1: annotatie via juridische_regel
    SELECT te.id AS tekst_element_id, ga.identificatie AS object_id
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
    JOIN p2p.juridische_regel_gebiedsaanwijzing jrg
         ON jrg.juridische_regel_id = jr.identificatie
    JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jrg.gebiedsaanwijzing_id
  UNION
    -- Route 2: IntIoRef → GIO → geometrie_identificatie → locatie → GA
    SELECT tir.tekst_element_id, ga.identificatie
    FROM p2p.tekst_inline_referentie tir
    JOIN p2p.geo_informatieobject gio
         ON gio.frbr_expression = tir.target_gio_expression
        AND gio.geometrie_identificatie IS NOT NULL
    JOIN p2p.locatie loc
         ON loc.geometrie_identificatie = gio.geometrie_identificatie
    JOIN p2p.gebiedsaanwijzing ga ON ga.locatie_id = loc.identificatie
    WHERE tir.soort = 'IntIoRef'
  UNION
    -- Route 3: naam-match (intra-regeling)
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
            -- Type 2: IntIoRef die via geometrie-UUID naar deze GA wijst
            SELECT 1
            FROM p2p.tekst_inline_referentie tir
            JOIN p2p.geo_informatieobject gio
                 ON gio.frbr_expression = tir.target_gio_expression
                AND gio.geometrie_identificatie IS NOT NULL
            JOIN p2p.locatie loc
                 ON loc.geometrie_identificatie = gio.geometrie_identificatie
            JOIN p2p.gebiedsaanwijzing ga2 ON ga2.locatie_id = loc.identificatie
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
        -- doelen_match: vergelijkt de geometrie-UUID die type 1 én type 2
        -- impliceren. Als beide aanwezig zijn én verschillende UUIDs aanwijzen
        -- → tegenstrijdig.
        (
            SELECT bool_and(loc1.geometrie_identificatie = gio.geometrie_identificatie)
            FROM p2p.gebiedsaanwijzing ga_match
            JOIN p2p.locatie loc1 ON loc1.identificatie = ga_match.locatie_id
            JOIN p2p.tekst_inline_referentie tir
              ON tir.tekst_element_id = k.tekst_element_id
             AND tir.soort = 'IntIoRef'
            JOIN p2p.geo_informatieobject gio
              ON gio.frbr_expression = tir.target_gio_expression
             AND gio.geometrie_identificatie IS NOT NULL
            WHERE ga_match.identificatie = k.object_id
              AND loc1.geometrie_identificatie IS NOT NULL
        ) AS doelen_match
    FROM ga_kandidaten k
),

-- Activiteit — type 2 NIET toepasselijk (Activiteit is geen IO)
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
        NULL::BOOLEAN AS has_intioref,         -- n.v.t. voor Activiteit
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_type = 'Activiteit'
              AND ns.object_id = k.object_id
        ) AS has_naammatch,
        NULL::BOOLEAN AS doelen_match          -- n.v.t.
    FROM act_kandidaten k
),

-- Omgevingsnorm + Omgevingswaarde — alle drie toepasselijk; type 2 via normwaarde
norm_kandidaten AS (
    -- Route 1: annotatie via juridische_regel_norm
    SELECT te.id AS tekst_element_id, n.identificatie AS object_id, n.norm_type
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
    JOIN p2p.juridische_regel_norm jrn
         ON jrn.juridische_regel_id = jr.identificatie
    JOIN p2p.norm n ON n.identificatie = jrn.norm_id
  UNION
    -- Route 2: IntIoRef → GIO → geometrie_identificatie → locatie → normwaarde → norm
    SELECT tir.tekst_element_id, n.identificatie, n.norm_type
    FROM p2p.tekst_inline_referentie tir
    JOIN p2p.geo_informatieobject gio
         ON gio.frbr_expression = tir.target_gio_expression
        AND gio.geometrie_identificatie IS NOT NULL
    JOIN p2p.locatie loc
         ON loc.geometrie_identificatie = gio.geometrie_identificatie
    JOIN p2p.normwaarde nw ON nw.locatie_id = loc.identificatie
    JOIN p2p.norm n ON n.identificatie = nw.norm_id
    WHERE tir.soort = 'IntIoRef'
  UNION
    -- Route 3: naam-match
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
            JOIN p2p.geo_informatieobject gio
                 ON gio.frbr_expression = tir.target_gio_expression
                AND gio.geometrie_identificatie IS NOT NULL
            JOIN p2p.locatie loc
                 ON loc.geometrie_identificatie = gio.geometrie_identificatie
            JOIN p2p.normwaarde nw ON nw.locatie_id = loc.identificatie
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
            -- Norm heeft via normwaarde meerdere locaties; doelen matchen
            -- als de IntIoRef-GIO's overeenkomen met ten minste één
            -- normwaarde.locatie van dit object.
            SELECT bool_and(EXISTS (
                SELECT 1
                FROM p2p.normwaarde nw2
                JOIN p2p.locatie loc2 ON loc2.identificatie = nw2.locatie_id
                WHERE nw2.norm_id = k.object_id
                  AND loc2.geometrie_identificatie = gio.geometrie_identificatie
            ))
            FROM p2p.tekst_inline_referentie tir
            JOIN p2p.geo_informatieobject gio
              ON gio.frbr_expression = tir.target_gio_expression
             AND gio.geometrie_identificatie IS NOT NULL
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
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
        -- tegenstrijdige_doelen: type 1 + type 2 beide aanwezig, GIO's verschillend
        WHEN has_annotatie AND COALESCE(has_intioref, FALSE) AND doelen_match = FALSE
            THEN 'tegenstrijdige_doelen'

        -- consistent_aanwezig: alle toepasselijke ✓
        WHEN has_annotatie
             AND COALESCE(has_intioref, TRUE)         -- TRUE als n.v.t.
             AND has_naammatch
            THEN 'consistent_aanwezig'

        -- vermoedelijk_vergeten_annotatie: type 1 ontbreekt, andere(n) aanwezig
        WHEN NOT has_annotatie
             AND (COALESCE(has_intioref, FALSE) OR has_naammatch)
            THEN 'vermoedelijk_vergeten_annotatie'

        -- annotatie_zonder_naam_match: alleen type 1, geen tekstuele steun
        WHEN has_annotatie
             AND NOT COALESCE(has_intioref, FALSE)
             AND NOT has_naammatch
            THEN 'annotatie_zonder_naam_match'

        -- consistent_aanwezig met deelaanwezigheid (lage prio gevallen)
        WHEN has_annotatie
            THEN 'consistent_aanwezig'

        ELSE 'overig_inconsistent'
    END AS consistentie_klasse
FROM alle_kandidaten;

COMMENT ON VIEW p2p.tekst_object_consistentie IS
    'v2 (2026-05-11) — per (tekst_element, object) één rij met de drie '
    'verwijsmechanismen + klasse-discriminator. Type-2-kandidaten en '
    'doelen_match lopen via gedeelde geometrie-UUID '
    '(geo_informatieobject.geometrie_identificatie ↔ locatie.geometrie_identificatie) '
    'i.p.v. via p2p.juridische_borging (zie G-69). Mono-mechanisme '
    'objecttypen (Ambtsgebied, Regelingsgebied) komen niet voor — Locatie-types, '
    'geen Activiteit/GA/Norm/Omgevingswaarde.';
