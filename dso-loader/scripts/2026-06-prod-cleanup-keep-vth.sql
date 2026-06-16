-- =============================================================================
-- PROD-OPSCHONING vóór GERICHTE restore — variant die `vth` BEHOUDT
-- =============================================================================
-- ⚠️  DESTRUCTIEF. Draai ALLEEN op de productie-Railway-DB, vlak vóór de
--     gerichte `pg_restore` (met `-N vth -N tiger`).
--
-- Verschil met 2026-06-prod-cleanup-before-restore.sql:
--   Die dropt ÁLLE app-schema's (incl. vth) → vergunningen-viewer down.
--   Deze BEHOUDT `vth` (+ de `koop`-view-alias) zodat /v1/vergunningen ONLINE
--   blijft tijdens de restore. Kan veilig omdat `vth` een eiland is: geen enkele
--   cross-schema foreign key naar of vanuit vth (geverifieerd 2026-06-10).
--
-- Behoudt: public (PostGIS), topology (postgis_topology), information_schema,
--          pg_*  én  vth + koop.
-- Dropt:   alle overige app-schema's (core, p2p, wro, i2a, v2a, p2pwijziging,
--          skos, …) zodat de gerichte restore op een schone lei landt.
--   De FK's i2a/p2p/wro/p2pwijziging → core lossen vanzelf op: alles wordt samen
--   gedropt en in dependency-volgorde herrestored.
-- =============================================================================

\echo '== Schema-inventaris VOOR opschoning =='
SELECT nspname AS schema,
       pg_size_pretty(COALESCE(sum(pg_total_relation_size(c.oid)), 0)) AS grootte
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','m','i','t')
WHERE n.nspname NOT IN ('information_schema')
  AND n.nspname NOT LIKE 'pg_%'
GROUP BY nspname
ORDER BY nspname;

DO $$
DECLARE
    s text;
    n int := 0;
BEGIN
    FOR s IN
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT IN ('public', 'information_schema', 'vth', 'koop', 'topology', 'tiger')
          AND nspname NOT LIKE 'pg_%'
    LOOP
        EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', s);
        RAISE NOTICE 'DROP SCHEMA % CASCADE', s;
        n := n + 1;
    END LOOP;
    RAISE NOTICE 'Totaal % app-schema(s) verwijderd. Behouden: vth, koop, public, topology.', n;
END $$;

\echo '== Schema-inventaris NA opschoning (vth/koop/public/topology moeten blijven) =='
SELECT nspname
FROM pg_namespace
WHERE nspname NOT IN ('information_schema')
  AND nspname NOT LIKE 'pg_%'
ORDER BY nspname;

\echo '== vth nog intact? (verwacht: vergunningkennisgeving telt 804.483) =='
SELECT count(*) AS vergunningen FROM vth.vergunningkennisgeving;

\echo '== PostGIS nog aanwezig? =='
SELECT extname, extversion FROM pg_extension WHERE extname = 'postgis';
