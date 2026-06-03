-- OCD API — read-only DB-user voor productie.
--
-- Draai éénmalig als `postgres` (of een ander superuser-account) op de
-- Railway PostGIS-database, NA de pg_restore. Daarna `DATABASE_URL` in
-- Railway switchen naar de ocd_reader-user.
--
-- Doel: defense-in-depth. Als ooit een SQL-injection sluipt of een
-- malicious payload de API bereikt, kan de connectie niets meer dan
-- SELECT doen. Geen DROP, geen DELETE, geen INSERT.

-- 1. User aanmaken met een sterk wachtwoord.
--    Vervang <STERK_WACHTWOORD> door bv. `openssl rand -base64 32`.
CREATE USER ocd_reader WITH PASSWORD '<STERK_WACHTWOORD>';

-- 2. Connect-rechten op de database.
--    `railway` is de default-databasename op Railway PostGIS.
GRANT CONNECT ON DATABASE railway TO ocd_reader;

-- 3. SELECT op alle relevante schemas. Voeg `vth` toe omdat
--    vergunningen.py daaruit leest (vergunningkennisgeving).
GRANT USAGE ON SCHEMA core, p2p, wro, i2a, v2a, vth TO ocd_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA core, p2p, wro, i2a, v2a, vth TO ocd_reader;

-- 4. Default-privileges zodat NIEUWE tabellen ook automatisch readable zijn
--    voor ocd_reader. Anders moet je dit script draaien na elke nieuwe table.
ALTER DEFAULT PRIVILEGES IN SCHEMA core, p2p, wro, i2a, v2a, vth
    GRANT SELECT ON TABLES TO ocd_reader;

-- 5. SELECT op alle sequences (voor RETURNING-clauses bij eventuele
--    toekomstige INSERTs — strict niet nodig voor read-only API, kan weg).
-- GRANT SELECT ON ALL SEQUENCES IN SCHEMA core, p2p, wro, i2a, v2a, vth TO ocd_reader;

-- 6. EXECUTE op SQL-functies (bv. `ocd_artikel_label` in main.py).
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA core, p2p, wro, i2a, v2a, vth TO ocd_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, p2p, wro, i2a, v2a, vth
    GRANT EXECUTE ON FUNCTIONS TO ocd_reader;

-- 7. PostGIS-functies zitten in `public`; geef ook daar usage + execute.
GRANT USAGE ON SCHEMA public TO ocd_reader;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO ocd_reader;

-- 8. Verifieer:
--    \du ocd_reader
--    SELECT current_user; -- als ocd_reader: zou 'ocd_reader' moeten zijn
--    SELECT count(*) FROM core.bronhouder; -- werkt
--    INSERT INTO core.bronhouder VALUES (...); -- moet falen met permission denied
