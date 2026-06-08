-- ============================================================================
-- 2026-06 · Data-health-laag: load-status + bronhouder-health-rapportage
--
-- Aanleiding: data-/loaderfouten obfusceerden retrieval-metingen (verwisselde
-- bronhouder-namen, half-geladen regelingen, dode geo-scopes). Doel is GRIP:
-- in één oogopslag per bronhouder zien of een lage meting door de DATA komt
-- of door de AANPAK — vóórdat een meting vervuild raakt.
--
-- Bevat:
--   1. p2p.regeling_load        — load-status per regeling (Fix C)
--   2. core.bronhouder_dso_diff — voeding uit diff_dso_bronhouder_coverage.py
--   3. core.mv_bronhouder_health — 1 rij per bronhouder met load/integriteit/
--                                  annotatie-metrieken (materialized)
--   4. core.v_data_health       — top-samenvatting met drempel-flags
--   5. core.v_geo_health        — globale geo-dekking (dode geo-scopes)
--
-- Idempotent: alle objecten via IF NOT EXISTS / CREATE OR REPLACE /
-- DROP ... IF EXISTS voor de matview.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-data-health.sql
-- Refresh matview later:  REFRESH MATERIALIZED VIEW core.mv_bronhouder_health;
-- ============================================================================

-- ── 1. Load-status per regeling (Fix C) ─────────────────────────────────────
-- Maakt stille load-fouten zichtbaar: de loader schrijft hier 1 rij per
-- regeling aan het eind van _load_from_zip. status:
--   'ok'       — tekst + (indien van toepassing) locaties geladen
--   'partieel' — regeling-rij bestaat maar 0 tekst_elementen
--   'gefaald'  — load-exception gevangen (laatste_fout gevuld)
CREATE TABLE IF NOT EXISTS p2p.regeling_load (
    frbr_expression text PRIMARY KEY
        REFERENCES p2p.regeling(frbr_expression) ON DELETE CASCADE,
    status          text NOT NULL DEFAULT 'ok',
    n_tekst         integer,
    n_locatie       integer,
    n_annotatie     integer,
    geladen_op      timestamptz NOT NULL DEFAULT now(),
    laatste_fout    text
);

-- Backfill uit de huidige tellingen, zodat de tabel meteen de bestaande
-- realiteit weergeeft (status afgeleid van tekst-aanwezigheid).
INSERT INTO p2p.regeling_load (frbr_expression, status, n_tekst, geladen_op)
SELECT r.frbr_expression,
       CASE WHEN coalesce(t.n, 0) = 0 THEN 'partieel' ELSE 'ok' END,
       coalesce(t.n, 0),
       now()
FROM p2p.regeling r
LEFT JOIN (SELECT regeling_expression, count(*) n
           FROM p2p.tekst_element GROUP BY 1) t
       ON t.regeling_expression = r.frbr_expression
ON CONFLICT (frbr_expression) DO UPDATE
    SET status  = EXCLUDED.status,
        n_tekst = EXCLUDED.n_tekst;

-- ── 2. DSO-vs-p2p coverage-diff (voeding uit diff-script) ────────────────────
-- diff_dso_bronhouder_coverage.py schrijft hier per bronhouder hoeveel
-- regelingen in DSO ontbreken lokaal (n_mist) of overbodig zijn (n_over).
-- De matview leest dit; leeg = NULL (nog niet gemeten).
CREATE TABLE IF NOT EXISTS core.bronhouder_dso_diff (
    overheidscode text PRIMARY KEY
        REFERENCES core.bronhouder(overheidscode) ON DELETE CASCADE,
    n_mist        integer NOT NULL DEFAULT 0,
    n_over        integer NOT NULL DEFAULT 0,
    gemeten_op    timestamptz NOT NULL DEFAULT now()
);

-- ── 3. Per-bronhouder health-matview ────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS core.mv_bronhouder_health;
CREATE MATERIALIZED VIEW core.mv_bronhouder_health AS
WITH te_per_reg AS (
    SELECT regeling_expression,
           count(*)                                    AS n_tekst,
           count(*) FILTER (WHERE element_type = 'Artikel') AS n_artikel
    FROM p2p.tekst_element
    GROUP BY 1
),
art_ann AS (   -- geannoteerde artikelen (juridische_regel op wid+regeling)
    SELECT te.regeling_expression,
           count(DISTINCT te.id) AS n_artikel_ann
    FROM p2p.tekst_element te
    JOIN p2p.juridische_regel jr
      ON jr.regeling_expression = te.regeling_expression
     AND jr.regeltekst_wid       = te.wid
    WHERE te.element_type = 'Artikel'
    GROUP BY 1
),
reg_per_bh AS (
    SELECT r.bronhouder,
           count(*) FILTER (WHERE NOT r.inactief)                                       AS n_regelingen,
           count(*) FILTER (WHERE NOT r.inactief AND coalesce(t.n_tekst, 0) = 0)         AS n_regelingen_zonder_tekst,
           sum(coalesce(t.n_artikel, 0))                                                 AS n_artikel,
           sum(coalesce(a.n_artikel_ann, 0))                                             AS n_artikel_ann
    FROM p2p.regeling r
    LEFT JOIN te_per_reg t ON t.regeling_expression = r.frbr_expression
    LEFT JOIN art_ann    a ON a.regeling_expression = r.frbr_expression
    GROUP BY 1
),
ala_per_bh AS (   -- annotatie-scope + kwalificatie-specificiteit per bronhouder
    SELECT r.bronhouder,
           count(*)                                                                       AS n_ala,
           count(*) FILTER (WHERE l.locatie_type IN ('Ambtsgebied', 'Gebiedengroep'))     AS n_breed,
           count(*) FILTER (WHERE a.kwalificatie = 'anders geduid')                       AS n_anders
    FROM p2p.activiteit_locatieaanduiding a
    JOIN p2p.juridische_regel jr ON jr.identificatie       = a.juridische_regel_id
    JOIN p2p.regeling         r  ON r.frbr_expression      = jr.regeling_expression
    JOIN p2p.locatie          l  ON l.identificatie        = a.locatie_id
    GROUP BY 1
),
dup AS (
    SELECT naam, bestuurslaag
    FROM core.bronhouder
    GROUP BY 1, 2
    HAVING count(*) > 1
)
SELECT
    b.overheidscode,
    b.naam,
    b.bestuurslaag,
    -- load
    coalesce(rb.n_regelingen, 0)               AS n_regelingen,
    coalesce(rb.n_regelingen_zonder_tekst, 0)  AS n_regelingen_zonder_tekst,
    d.n_mist                                   AS dso_mist,
    d.n_over                                   AS dso_over,
    -- naam-integriteit
    (b.naam = b.overheidscode OR b.naam ~ '^[0-9]+$')                                    AS is_code_only,
    EXISTS (SELECT 1 FROM dup WHERE dup.naam = b.naam
            AND dup.bestuurslaag IS NOT DISTINCT FROM b.bestuurslaag)                     AS is_duplicate_naam,
    (b.bestuurslaag = 'gemeente'
        AND EXISTS (SELECT 1 FROM core.gemeentegrens g
                    WHERE g.overheidscode = b.overheidscode
                      AND lower(g.naam) <> lower(b.naam)))                                AS pdok_mismatch,
    -- annotatiedichtheid
    CASE WHEN coalesce(rb.n_artikel, 0) > 0
         THEN round(100.0 * rb.n_artikel_ann / rb.n_artikel, 1) END                      AS artikel_dekking_pct,
    CASE WHEN coalesce(ab.n_ala, 0) > 0
         THEN round(100.0 * ab.n_breed / ab.n_ala, 1) END                                AS pct_brede_scope,
    CASE WHEN coalesce(ab.n_ala, 0) > 0
         THEN round(100.0 * ab.n_anders / ab.n_ala, 1) END                               AS pct_anders_geduid
FROM core.bronhouder b
LEFT JOIN reg_per_bh           rb ON rb.bronhouder   = b.overheidscode
LEFT JOIN ala_per_bh           ab ON ab.bronhouder   = b.overheidscode
LEFT JOIN core.bronhouder_dso_diff d ON d.overheidscode = b.overheidscode;

CREATE UNIQUE INDEX IF NOT EXISTS mv_bronhouder_health_pk
    ON core.mv_bronhouder_health (overheidscode);

-- ── 4. Top-samenvatting met drempel-flags ───────────────────────────────────
CREATE OR REPLACE VIEW core.v_data_health AS
SELECT
    count(*)                                                       AS bronhouders,
    count(*) FILTER (WHERE n_regelingen > 0)                       AS bronhouders_met_content,
    -- naam-integriteit (alleen bronhouders met content tellen mee voor flags)
    count(*) FILTER (WHERE is_code_only      AND n_regelingen > 0) AS code_only_met_content,
    count(*) FILTER (WHERE is_duplicate_naam)                      AS duplicate_naam,
    count(*) FILTER (WHERE pdok_mismatch)                          AS pdok_mismatch,
    -- load
    sum(n_regelingen_zonder_tekst)                                AS regelingen_zonder_tekst,
    coalesce(sum(dso_mist), 0)                                    AS dso_mist_totaal,
    -- annotatiedichtheid (gewogen gemiddelde over bronhouders met content)
    round(avg(artikel_dekking_pct) FILTER (WHERE artikel_dekking_pct IS NOT NULL), 1) AS avg_artikel_dekking_pct,
    round(avg(pct_brede_scope)     FILTER (WHERE pct_brede_scope     IS NOT NULL), 1) AS avg_pct_brede_scope,
    round(avg(pct_anders_geduid)   FILTER (WHERE pct_anders_geduid   IS NOT NULL), 1) AS avg_pct_anders_geduid
FROM core.mv_bronhouder_health;

-- ── 5. Globale geo-dekking (dode geo-scopes) ────────────────────────────────
-- Een "dode geo-scope" = vindbare locatie (heeft subdiv) zonder enige
-- annotatie-link. Hoog aantal hoeft geen blinde vlek te zijn (containers
-- liggen onder geannoteerde peers) — maar een PLOTSE stijging is een signaal.
CREATE OR REPLACE VIEW core.v_geo_health AS
WITH findable AS (SELECT DISTINCT identificatie FROM p2p.locatie_subdiv),
linked AS (
    SELECT locatie_id FROM p2p.tekstdeel WHERE locatie_id IS NOT NULL
    UNION SELECT locatie_id FROM p2p.gebiedsaanwijzing
    UNION SELECT locatie_id FROM p2p.normwaarde
    UNION SELECT locatie_id FROM p2p.activiteit_locatieaanduiding
    UNION SELECT locatie_id FROM p2p.pons
)
SELECT
    (SELECT count(*) FROM findable)                                          AS vindbare_locaties,
    (SELECT count(*) FROM findable f
       WHERE NOT EXISTS (SELECT 1 FROM linked l WHERE l.locatie_id = f.identificatie)) AS dode_geo_scopes,
    (SELECT count(*) FROM p2p.locatie_subdiv WHERE geometrie IS NULL)        AS subdiv_geometrie_null,
    (SELECT count(*) FROM p2p.locatie_subdiv s
       WHERE NOT EXISTS (SELECT 1 FROM p2p.locatie l WHERE l.identificatie = s.identificatie)) AS subdiv_orphans,
    (SELECT count(*) FROM p2p.locatiegroep_lid)                              AS locatiegroep_lid_rijen;
