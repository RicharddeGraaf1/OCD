-- ============================================================================
-- 2026-07-18 · MER-bronnen toevoegen aan het data-actualiteit-dashboard.
-- Breidt core.bron_totaal + core.v_bron_totalen uit met:
--   mer-events    -> mer.event    (Kanaal A, KOOP SRU)
--   mer-commissie -> mer.project  (Kanaal B, Commissie m.e.r.)
-- Prod-veilig: EXCEPTION → NULL wanneer het mer-schema (nog) niet bestaat.
-- Run: psql ... -f scripts/2026-07-18-mer-bron-totaal.sql
-- ============================================================================

CREATE OR REPLACE FUNCTION core.bron_totaal(p_bron text)
RETURNS bigint LANGUAGE plpgsql STABLE AS $fn$
DECLARE q text; n bigint;
BEGIN
    q := CASE p_bron
        WHEN 'ozon-regelingen'         THEN $q$SELECT count(*) FROM p2p.regeling$q$
        WHEN 'ozon-besluitversies'     THEN $q$SELECT count(*) FROM p2pwijziging.besluit WHERE soort = 'besluitversie'$q$
        WHEN 'ozon-ontwerpen'          THEN $q$SELECT count(*) FROM p2pwijziging.besluit WHERE soort = 'ontwerp'$q$
        WHEN 'ozon-afwijkvergunningen' THEN $q$SELECT count(*) FROM vth.omgevingsvergunning_dso$q$
        WHEN 'rtr-toepasbare-regels'   THEN $q$SELECT count(*) FROM i2a.regelbeheerobject$q$
        WHEN 'koop-sru-vergunningen'   THEN $q$SELECT count(*) FROM vth.vergunningkennisgeving$q$
        WHEN 'obk-vergunningen-inhoud' THEN $q$SELECT count(*) FROM vth.vergunningkennisgeving WHERE inhoud_geladen_at IS NOT NULL$q$
        WHEN 'ihr-plannen'             THEN $q$SELECT count(*) FROM wro.wro_tekst_object$q$
        WHEN 'ihr-planvoorraad'        THEN $q$SELECT count(*) FROM wro.wro_plan_observatie o WHERE o.snapshot_id = (SELECT snapshot_id FROM wro.wro_snapshot ORDER BY datum DESC LIMIT 1)$q$
        WHEN 'pdok-bestemmingsplannen' THEN $q$SELECT count(*) FROM wro.ruimtelijk_instrument WHERE type_plan IS DISTINCT FROM 'structuurvisie'$q$
        WHEN 'pdok-structuurvisies'    THEN $q$SELECT count(*) FROM wro.ruimtelijk_instrument WHERE type_plan = 'structuurvisie'$q$
        WHEN 'pdok-gemeentegrenzen'    THEN $q$SELECT count(*) FROM core.gemeentegrens$q$
        WHEN 'mer-events'              THEN $q$SELECT count(*) FROM mer.event$q$
        WHEN 'mer-commissie'           THEN $q$SELECT count(*) FROM mer.project$q$
        ELSE NULL
    END;
    IF q IS NULL THEN RETURN NULL; END IF;
    EXECUTE q INTO n;
    RETURN n;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function OR invalid_schema_name THEN
    RETURN NULL;
END;
$fn$;

CREATE OR REPLACE VIEW core.v_bron_totalen AS
SELECT b AS bron, core.bron_totaal(b) AS totaal
FROM unnest(ARRAY[
    'ozon-regelingen','ozon-besluitversies','ozon-ontwerpen','ozon-afwijkvergunningen',
    'rtr-toepasbare-regels','koop-sru-vergunningen','obk-vergunningen-inhoud',
    'ihr-plannen','ihr-planvoorraad','pdok-bestemmingsplannen',
    'pdok-structuurvisies','pdok-gemeentegrenzen',
    'mer-events','mer-commissie'
]) AS b;
