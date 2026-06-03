-- =============================================================================
-- Migratie: hernoem schema  koop  ->  vth
-- =============================================================================
-- Datum: 2026-06-03
--
-- Achtergrond: het schema heette `koop` naar de bron (KOOP /
-- officielebekendmakingen), maar dekt inhoudelijk het VTH-domein
-- (vergunningverlening, toezicht, handhaving). `vth` is bron-onafhankelijk
-- en dekt ook toekomstige niet-KOOP-bronnen.
--
-- Het schema is LOSSTAAND (geen FK's naar core/p2p/wro/i2a/v2a), dus één
-- ALTER volstaat: tabellen, indexen, FK's en constraints binnen het schema
-- verhuizen automatisch mee. Bestaande grants (ocd_reader: USAGE/SELECT)
-- blijven gelden — ze hangen aan het schema-object, niet aan de naam.
--
-- Idempotent: veilig om meermaals te draaien.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'vth') THEN
        RAISE NOTICE 'Schema "vth" bestaat al — geen actie.';
    ELSIF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'koop') THEN
        EXECUTE 'ALTER SCHEMA koop RENAME TO vth';
        RAISE NOTICE 'Schema "koop" hernoemd naar "vth".';
    ELSE
        RAISE NOTICE 'Schema "koop" niet gevonden en "vth" bestaat niet — geen actie.';
    END IF;
END $$;

-- Verificatie (handmatig na afloop):
--   SELECT count(*) FROM vth.vergunningkennisgeving;
--   SELECT count(*) FROM vth.vergunning_deeplink;
--   SELECT * FROM vth.etl_run ORDER BY processed_date DESC LIMIT 5;
