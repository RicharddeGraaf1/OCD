-- ============================================================================
-- 2026-07 · Load-run-log: core.load_run
--
-- Aanleiding: er is geen consistent bijgehouden "wanneer is wat geladen"-
-- registratie. p2p.regeling_load.geladen_op is fijnmazig maar geen overzicht;
-- core.bronhouder.laatst_geladen bestaat maar wordt niet gevuld; vth.etl_run
-- dekt alleen KOOP. Deze tabel generaliseert dat: 1 rij per laad-stap
-- (= 1 CLI-commando = 1 `bron`-waarde), voor het data-actualiteit-dashboard.
-- Plan: OCDviewer/docs/plans/data-health-dashboard.md (fase 1).
--
-- Fase 2 hangt src/run_log.py om elke load-*-command: 'running'-rij bij start,
-- bijgewerkt naar 'ok'/'deels'/'gefaald' bij afloop.
--
-- Canonieke `bron`-waarden (vrije tekst, bewust geen CHECK):
--   ozon-regelingen · ozon-besluitversies · ozon-ontwerpen · ozon-afwijkvergunningen
--   rtr-toepasbare-regels · koop-sru-vergunningen · obk-vergunningen-inhoud
--   ihr-plannen · ihr-planvoorraad · pdok-bestemmingsplannen
--   pdok-structuurvisies · pdok-gemeentegrenzen
--
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS; backfill alleen als er nog geen
-- rij voor die bron bestaat.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-07-add-load-run.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS core.load_run (
    run_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bron        TEXT NOT NULL,
    scope       TEXT NULL,                       -- 'alle' | overheidscode | gemeente | datumbereik
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL,
    status      TEXT NOT NULL DEFAULT 'running', -- running | ok | deels | gefaald
    n_verwerkt  INTEGER NULL,
    n_fout      INTEGER NOT NULL DEFAULT 0,
    details     JSONB NULL,
    error       TEXT NULL,
    CONSTRAINT load_run_status_chk
        CHECK (status IN ('running', 'ok', 'deels', 'gefaald'))
);

CREATE INDEX IF NOT EXISTS idx_load_run_bron_started
    ON core.load_run (bron, started_at DESC);

-- Leeslaag (fase 3): laatste run per bron. Voedt /v1/load-status + dashboard.
CREATE OR REPLACE VIEW core.v_load_status AS
SELECT DISTINCT ON (bron)
    bron, scope, started_at, finished_at, status, n_verwerkt, n_fout
FROM core.load_run
ORDER BY bron, started_at DESC;

-- Totaal-aantal per bron (live telling van de doel-tabel). Naast n_verwerkt
-- ('bijgewerkt in de laatste run') geeft dit 'hoeveel staat er nu in totaal'.
-- Prod-veilig: dynamische EXECUTE → NULL als een tabel/schema ontbreekt
-- (bv. p2pwijziging uitgesloten bij een restore). Tel-bronnen geverifieerd
-- tegen de dev-DB (2026-07-04).
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
        ELSE NULL
    END;
    IF q IS NULL THEN RETURN NULL; END IF;
    EXECUTE q INTO n;
    RETURN n;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function OR invalid_schema_name THEN
    RETURN NULL;
END;
$fn$;

-- Totaal per canonieke bron (voor het dashboard). Eén rij per bron.
CREATE OR REPLACE VIEW core.v_bron_totalen AS
SELECT b AS bron, core.bron_totaal(b) AS totaal
FROM unnest(ARRAY[
    'ozon-regelingen','ozon-besluitversies','ozon-ontwerpen','ozon-afwijkvergunningen',
    'rtr-toepasbare-regels','koop-sru-vergunningen','obk-vergunningen-inhoud',
    'ihr-plannen','ihr-planvoorraad','pdok-bestemmingsplannen',
    'pdok-structuurvisies','pdok-gemeentegrenzen'
]) AS b;

-- ── Backfill (best-effort, uit échte bestaande timestamps) ───────────────────
-- Zodat het dashboard niet leeg start. Leest alleen bronnen waarvoor een
-- betrouwbare timestamp al in de DB staat; de rest vult zich vanaf de eerste
-- load onder fase 2. `details.backfill = true` markeert deze afgeleide rijen.

-- ozon-regelingen ← p2p.regeling_load (laatste load + tellingen)
INSERT INTO core.load_run (bron, scope, started_at, finished_at, status, n_verwerkt, n_fout, details)
SELECT 'ozon-regelingen', 'alle',
       max(geladen_op), max(geladen_op),
       CASE WHEN count(*) FILTER (WHERE status <> 'ok') > 0 THEN 'deels' ELSE 'ok' END,
       count(*),
       count(*) FILTER (WHERE status <> 'ok'),
       jsonb_build_object('backfill', true, 'herkomst', 'p2p.regeling_load')
FROM p2p.regeling_load
HAVING count(*) > 0
   AND NOT EXISTS (SELECT 1 FROM core.load_run WHERE bron = 'ozon-regelingen');

-- koop-sru-vergunningen ← vth.etl_run (laatste geslaagde SRU-load + som records)
INSERT INTO core.load_run (bron, scope, started_at, finished_at, status, n_verwerkt, n_fout, details)
SELECT 'koop-sru-vergunningen', 'alle',
       max(finished_at), max(finished_at),
       CASE WHEN count(*) FILTER (WHERE status <> 'ok') > 0 THEN 'deels' ELSE 'ok' END,
       coalesce(sum(record_count), 0),
       count(*) FILTER (WHERE status <> 'ok'),
       jsonb_build_object('backfill', true, 'herkomst', 'vth.etl_run')
FROM vth.etl_run
HAVING count(*) > 0
   AND NOT EXISTS (SELECT 1 FROM core.load_run WHERE bron = 'koop-sru-vergunningen');
