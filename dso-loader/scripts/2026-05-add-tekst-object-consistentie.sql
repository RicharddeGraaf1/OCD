-- ============================================================================
-- 2026-05 · Tekst↔object consistentie-view (drieslag — alle drie samen)
--
-- View die per (tekst_element, object) de drie verwijsmechanismen
-- evalueert en een consistentie-klasse toekent. Doel: integriteits-
-- signaal voor de OCD-viewer (vermoedelijk vergeten annotatie,
-- annotatie zonder tekstbasis, tegenstrijdige doelen, etc.).
--
-- Klasse-mapping (zie vault_v1 plan-pagina):
--   has_1 / has_2 / has_3 / doelen_match → klasse
--   ✓ ✓ ✓ ✓                 → consistent_aanwezig
--   ✓ ✓ ✗ ✓                 → consistent_aanwezig (lage prio)
--   ✓ ✗ ✓ n.v.t.            → consistent_aanwezig (geen GIO-tekst)
--   ✗ ✗ ✗ n.v.t.            → consistent_afwezig (komt niet in view voor — niet kandidaat)
--   ✗ * ✓ n.v.t.            → vermoedelijk_vergeten_annotatie
--   ✗ ✓ ✗ n.v.t.            → vermoedelijk_vergeten_annotatie
--   ✓ ✗ ✗ n.v.t.            → annotatie_zonder_naam_match (NIET interpreteren
--                              als kwaliteitsfout voor Activiteit — vele
--                              Activiteit-namen zijn classificatorisch met
--                              suffix "gereguleerd in het omgevingsplan", die
--                              komen letterlijk niet in regelteksten voor.
--                              Voor GA/Norm/Omgevingswaarde wel betrouwbaar
--                              signaal. Zie [[Drie verwijsmechanismen tekst-object]].)
--   ✓ ✓ * ✗                 → tegenstrijdige_doelen
--
-- Mono-mechanisme uitsluitingen (alleen type 1 toepasselijk):
--   Ambtsgebied en Regelingsgebied — GEEN object_type in deze view;
--   ze komen voor als Locatie-types, niet als zelfstandige objecten in
--   de drie kolommen Activiteit/GA/Norm/Omgevingswaarde. Geen filter
--   nodig hier; dat valt automatisch buiten scope.
--
-- Achtergrond: stap 3 in
--   vault_v1/analysis/Plan implementatie drieslag tekst-object.md
--
-- Idempotent: CREATE OR REPLACE VIEW.
-- Vereist: tekst_inline_referentie (stap 1) + naammatch_signaal (stap 2).
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-object-consistentie.sql
-- ============================================================================

CREATE OR REPLACE VIEW p2p.tekst_object_consistentie AS

-- ─── Per objecttype: alle (tekst_element, object) kandidaten + booleans ───

-- Gebiedsaanwijzing — alle drie mechanismen toepasselijk
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
    JOIN p2p.juridische_borging jb ON jb.gio_expression = tir.target_gio_expression
    JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jb.domein_object_id
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
            JOIN p2p.juridische_borging jb ON jb.gio_expression = tir.target_gio_expression
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
              AND jb.domein_object_id = k.object_id
        ) AS has_intioref,
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_type = 'Gebiedsaanwijzing'
              AND ns.object_id = k.object_id
        ) AS has_naammatch,
        -- doelen_match: voor type 1 + type 2 vergelijk de GIO's
        (
            SELECT bool_and(jb_ann.gio_expression = tir.target_gio_expression)
            FROM p2p.juridische_borging jb_ann
            JOIN p2p.tekst_inline_referentie tir
              ON tir.tekst_element_id = k.tekst_element_id
             AND tir.soort = 'IntIoRef'
            WHERE jb_ann.domein_object_id = k.object_id
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

-- Omgevingsnorm + Omgevingswaarde — alle drie toepasselijk
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
    JOIN p2p.juridische_borging jb ON jb.gio_expression = tir.target_gio_expression
    JOIN p2p.norm n ON n.identificatie = jb.domein_object_id
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
            JOIN p2p.juridische_borging jb ON jb.gio_expression = tir.target_gio_expression
            WHERE tir.tekst_element_id = k.tekst_element_id
              AND tir.soort = 'IntIoRef'
              AND jb.domein_object_id = k.object_id
        ) AS has_intioref,
        EXISTS (
            SELECT 1 FROM p2p.naammatch_signaal_intra ns
            WHERE ns.tekst_element_id = k.tekst_element_id
              AND ns.object_id = k.object_id
              AND ns.object_type IN ('Omgevingsnorm', 'Omgevingswaarde')
        ) AS has_naammatch,
        (
            SELECT bool_and(jb_ann.gio_expression = tir.target_gio_expression)
            FROM p2p.juridische_borging jb_ann
            JOIN p2p.tekst_inline_referentie tir
              ON tir.tekst_element_id = k.tekst_element_id
             AND tir.soort = 'IntIoRef'
            WHERE jb_ann.domein_object_id = k.object_id
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
    'Per (tekst_element, object) één rij met de drie verwijsmechanismen + '
    'klasse-discriminator. Filter de view op consistentie_klasse om '
    'integriteitssignalen aan een redacteur te tonen. Mono-mechanisme '
    'objecttypen (Ambtsgebied, Regelingsgebied) komen niet voor in de view '
    'omdat ze geen Activiteit/GA/Norm/Omgevingswaarde zijn maar Locatie-types.';
