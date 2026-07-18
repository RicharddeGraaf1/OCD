-- ─────────────────────────────────────────────────────────────────────
-- Pre-sync voorbereiding (2026-07-17)
--
-- 1. audit-schema met historie-tabellen zodat de actualiteitsgegevens
--    van een vorige sync (regeling_load, bronhouder-status, health)
--    bewaard blijven wanneer een nieuwe sync ze overschrijft.
-- 2. Dedup van p2p.activiteit_locatieaanduiding en p2p.normwaarde:
--    deze inserts hadden geen ON CONFLICT, dus eerdere herlaads hebben
--    identieke tupels dubbel ingeschoten (118k+ groepen per 2026-07-17).
-- 3. Unieke indexen die herhaling structureel onmogelijk maken; de
--    loader gebruikt vanaf nu ON CONFLICT DO NOTHING (api_loader.py).
--
-- Idempotent: veilig om vaker te draaien.
-- ─────────────────────────────────────────────────────────────────────

-- ── 1. audit-schema + historie-tabellen ──────────────────────────────

CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.sync_run (
    run_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label       TEXT NOT NULL,
    gestart_op  TIMESTAMPTZ NOT NULL DEFAULT now(),
    klaar_op    TIMESTAMPTZ,
    opmerking   TEXT
);

-- Totaal-momentopname per run (per-bron aantallen + kern-metrics), zodat het
-- dashboard per run kan tonen hoeveel er t.o.v. de vorige run is veranderd.
-- JSONB: totalen = {bron: aantal}, metrics = {regelingen, inactief, db_grootte, ...}
ALTER TABLE audit.sync_run ADD COLUMN IF NOT EXISTS totalen JSONB;
ALTER TABLE audit.sync_run ADD COLUMN IF NOT EXISTS metrics JSONB;

-- Snapshot van p2p.regeling_load per sync-run (geladen_op-historie).
CREATE TABLE IF NOT EXISTS audit.regeling_load_hist (
    LIKE p2p.regeling_load,
    run_id      BIGINT,
    snapshot_op TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Snapshot van core.bronhouder laad-status per sync-run.
CREATE TABLE IF NOT EXISTS audit.bronhouder_status_hist (
    LIKE core.bronhouder,
    run_id      BIGINT,
    snapshot_op TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Snapshot van de health-matview per sync-run (trend-reporting).
CREATE TABLE IF NOT EXISTS audit.bronhouder_health_hist AS
    SELECT h.*, NULL::bigint AS run_id, now() AS snapshot_op
    FROM core.mv_bronhouder_health h LIMIT 0;

-- ── 2. Dedup ALA + normwaarde ────────────────────────────────────────

DELETE FROM p2p.activiteit_locatieaanduiding
WHERE id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (
                   PARTITION BY juridische_regel_id, activiteit_id,
                                locatie_id, kwalificatie
                   ORDER BY id) AS rn
        FROM p2p.activiteit_locatieaanduiding) t
    WHERE t.rn > 1);

DELETE FROM p2p.normwaarde
WHERE id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (
                   PARTITION BY norm_id, locatie_id, kwalitatieve_waarde,
                                kwantitatieve_waarde, waarde_in_regeltekst
                   ORDER BY id) AS rn
        FROM p2p.normwaarde) t
    WHERE t.rn > 1);

-- ── 3. Unieke indexen (PG16: NULLS NOT DISTINCT) ─────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS uq_ala_natural
    ON p2p.activiteit_locatieaanduiding
       (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
    NULLS NOT DISTINCT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_normwaarde_natural
    ON p2p.normwaarde
       (norm_id, locatie_id, kwalitatieve_waarde,
        kwantitatieve_waarde, waarde_in_regeltekst)
    NULLS NOT DISTINCT;

ANALYZE p2p.activiteit_locatieaanduiding;
ANALYZE p2p.normwaarde;
