-- ============================================================================
-- 2026-05 · naammatch_signaal_intra — intra-regeling-filter op v1
--
-- p2p.naammatch_signaal v1 (eerste poging, inter-regeling) bevat 5,1M rijen
-- omdat dezelfde object-naam in 100+ gemeenten voorkomt en we per
-- (object_id, tekst_element) joinen ongeacht of ze bij dezelfde regeling
-- horen. Voor de drieslag-consistentie-check is dat semantisch onbruikbaar
-- (een tekst van gemeente A matcht zo met een Activiteit van gemeente B).
--
-- Eerste poging om de filter direct in de v1-DDL in te bakken duurde >1u
-- (gecanceld) — de planner kon de extra JOIN-keten niet snel optimaliseren.
-- Deze v2-aanpak is veel sneller: we hergebruiken de v1-matview als input
-- en filteren erbovenop. Build duurt ~5 seconden i.p.v. >1 uur.
--
-- Achtergrond: gebruiker-keuze 2026-05-08 (optie 1 met praktische twist).
-- Refresh-volgorde:
--   1. REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal       -- ~35 min
--   2. REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal_intra -- ~5 sec
--
-- Idempotent: DROP IF EXISTS + CREATE.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-naammatch-signaal-intra.sql
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS p2p.naammatch_signaal_intra CASCADE;

CREATE MATERIALIZED VIEW p2p.naammatch_signaal_intra AS
WITH obj_regeling AS (
    -- Per object: in welke regeling(en) wordt het via de juridische-regel-laag gebruikt?
    SELECT DISTINCT ga.identificatie AS object_id,
           'Gebiedsaanwijzing'::TEXT AS object_type,
           te.regeling_expression
    FROM p2p.gebiedsaanwijzing ga
    JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.gebiedsaanwijzing_id = ga.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = jrg.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid

  UNION ALL

    SELECT DISTINCT a.identificatie,
           'Activiteit',
           te.regeling_expression
    FROM p2p.activiteit a
    JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid

  UNION ALL

    SELECT DISTINCT n.identificatie,
           CASE n.norm_type WHEN 'Omgevingswaarde' THEN 'Omgevingswaarde' ELSE 'Omgevingsnorm' END,
           te.regeling_expression
    FROM p2p.norm n
    JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
    JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
    JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
)
SELECT
    ns.tekst_element_id,
    ns.object_id,
    ns.object_type,
    ns.gematchte_naam,
    te.regeling_expression
FROM p2p.naammatch_signaal ns
JOIN p2p.tekst_element te ON te.id = ns.tekst_element_id
JOIN obj_regeling oRR
  ON oRR.object_id = ns.object_id
 AND oRR.object_type = ns.object_type
 AND oRR.regeling_expression = te.regeling_expression;

-- UNIQUE index voor REFRESH CONCURRENTLY
CREATE UNIQUE INDEX naammatch_signaal_intra_pk
    ON p2p.naammatch_signaal_intra (tekst_element_id, object_type, object_id);

CREATE INDEX naammatch_signaal_intra_object
    ON p2p.naammatch_signaal_intra (object_type, object_id);

CREATE INDEX naammatch_signaal_intra_te
    ON p2p.naammatch_signaal_intra (tekst_element_id);

CREATE INDEX naammatch_signaal_intra_regeling
    ON p2p.naammatch_signaal_intra (regeling_expression);

COMMENT ON MATERIALIZED VIEW p2p.naammatch_signaal_intra IS
    'Intra-regeling-filter op p2p.naammatch_signaal. Bevat alleen naam-matches '
    'waar object en tekst_element bij DEZELFDE regeling horen. Type 3 in drieslag '
    'tekst↔object — dit is de versie die in de consistentie-view gebruikt moet '
    'worden. naammatch_signaal (v1) blijft beschikbaar voor cross-regeling-analyses.';
