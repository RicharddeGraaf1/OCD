-- =============================================================================
-- PROD-OPSCHONING vóór volledige restore van de dev-DB
-- =============================================================================
-- ⚠️  DESTRUCTIEF. Draai dit ALLEEN op de productie-Railway-DB, vlak vóór
--     `pg_restore` van de dev-dump. Het verwijdert alle applicatie-schema's
--     (incl. het oude/partiële `koop` en de partiële `p2p`/`v2a`) zodat de
--     restore op een schone lei landt en er geen stale duplicaten blijven.
--
-- Behoudt: `public` (daar leeft de PostGIS-extensie), `information_schema`,
--          en alle `pg_*`-systeemschema's.
--
-- Waarom dynamisch i.p.v. een vaste DROP-lijst: we weten niet 100% zeker welke
-- app-schema's de huidige (partiële) prod-DB heeft (koop/core/p2p/v2a/…), en de
-- dev-dump hermaakt ze allemaal via CREATE SCHEMA. Alles-behalve-systeem droppen
-- is daarom het veiligst en meest voorspelbaar.
--
-- De `vth`-naam komt vanzelf mee uit de dump (dev is daar al hernoemd) — er is
-- dus GEEN aparte ALTER SCHEMA koop RENAME TO vth op prod nodig.
-- =============================================================================

\echo '== Schema-inventaris VOOR opschoning =='
SELECT nspname AS schema,
       pg_size_pretty(COALESCE(sum(pg_total_relation_size(c.oid)), 0)) AS grootte
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','m','i','t')
WHERE n.nspname NOT IN ('public','information_schema')
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
        WHERE nspname NOT IN ('public', 'information_schema')
          AND nspname NOT LIKE 'pg_%'
    LOOP
        EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', s);
        RAISE NOTICE 'DROP SCHEMA % CASCADE', s;
        n := n + 1;
    END LOOP;
    RAISE NOTICE 'Totaal % app-schema(s) verwijderd.', n;
END $$;

\echo '== Schema-inventaris NA opschoning (zou leeg moeten zijn op public na) =='
SELECT nspname
FROM pg_namespace
WHERE nspname NOT IN ('public','information_schema')
  AND nspname NOT LIKE 'pg_%'
ORDER BY nspname;

\echo '== PostGIS nog aanwezig? =='
SELECT extname, extversion FROM pg_extension WHERE extname = 'postgis';
