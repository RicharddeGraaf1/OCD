-- 2026-09-02 — Junctietabel voor de M:N-relatie werkzaamheid ↔ activiteit.
--
-- AANLEIDING
-- `i2a.werkzaamheid` heeft één `activiteit_id`-kolom, terwijl de RTR mediaan
-- 356 koppelingen per werkzaamheid levert (min 11, max 722; gemeten over 25
-- werkzaamheden op 2026-09-01). `_load_werkzaamheden` in imtr_loader.py liep
-- door de koppelingen heen, deed UPDATE ... SET activiteit_id = <de eerste die
-- toevallig al in p2p.activiteit stond> en daarna `break`. Gevolg: 294 rijen
-- die naar 70 unieke activiteiten wijzen, allemaal gemeentelijk en semantisch
-- onhoudbaar — "Asfaltcentrale" → nl.imow-gm0160.activiteit.BouwwerkGebruiken.
--
-- Doordat de EXISTS-check als koppelvóórwaarde fungeerde, landde bovendien
-- alles op de bronhouder die het eerst geladen was.
--
-- Zie het kennismodel-register: gaps#G-136.
--
-- WAT DIT SCRIPT DOET
-- 1. Nieuwe tabel i2a.werkzaamheid_activiteit (de echte M:N).
-- 2. `gezien_in_p2p` als kwaliteitsvlag i.p.v. koppelvoorwaarde: we bewaren
--    ALLE koppelingen die de RTR geeft, ook als de activiteit (nog) niet in
--    p2p.activiteit staat. Dat is een dekkingssignaal, geen reden om te
--    vergeten. Vandaar ook geen foreign key naar p2p.activiteit.
-- 3. De oude kolom i2a.werkzaamheid.activiteit_id blijft staan maar wordt
--    gemarkeerd als deprecated; hij wordt in een aparte stap verwijderd zodra
--    niets er meer op leest.
--
-- IDEMPOTENT: veilig herhaalbaar (CREATE ... IF NOT EXISTS).
-- Draaien:  psql "$DB_URL" -v ON_ERROR_STOP=1 -f 2026-09-add-werkzaamheid-activiteit-junctie.sql

BEGIN;

CREATE TABLE IF NOT EXISTS i2a.werkzaamheid_activiteit (
    werkzaamheid_urn  text    NOT NULL
        REFERENCES i2a.werkzaamheid (urn) ON DELETE CASCADE,
    activiteit_urn    text    NOT NULL,
    -- Kwaliteitsvlag, GEEN filter: staat deze activiteit ook in p2p.activiteit?
    -- Wordt bij het laden gezet en bij een herberekening bijgewerkt.
    gezien_in_p2p     boolean NOT NULL DEFAULT false,
    -- Bestuurslaag afgeleid uit de URN-namespace (gm/pv/ws/mnre). De RTR levert
    -- geen bevoegd-gezag-veld op het koppeling-object; alleen `urn` en `_links`.
    bestuurslaag      text,
    overheid_ns       text,
    geladen_op        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (werkzaamheid_urn, activiteit_urn)
);

COMMENT ON TABLE i2a.werkzaamheid_activiteit IS
    'M:N werkzaamheid <-> activiteit uit RTR /werkzaamheden/{urn}/activiteitKoppelingen. '
    'Mediaan ~356 rijen per werkzaamheid. gezien_in_p2p is een kwaliteitsvlag, geen filter.';
COMMENT ON COLUMN i2a.werkzaamheid_activiteit.gezien_in_p2p IS
    'Staat activiteit_urn in p2p.activiteit? False = dekkingsgat, niet: overslaan.';
COMMENT ON COLUMN i2a.werkzaamheid_activiteit.overheid_ns IS
    'Namespace uit de URN, bv. gm0344 — de RTR geeft geen bevoegd gezag mee.';

CREATE INDEX IF NOT EXISTS idx_wz_act_activiteit
    ON i2a.werkzaamheid_activiteit (activiteit_urn);
CREATE INDEX IF NOT EXISTS idx_wz_act_overheid
    ON i2a.werkzaamheid_activiteit (overheid_ns);
-- Voor de ingang van de werkzaamheid-pagina: werkzaamheid x overheid -> activiteit.
CREATE INDEX IF NOT EXISTS idx_wz_act_wz_overheid
    ON i2a.werkzaamheid_activiteit (werkzaamheid_urn, overheid_ns);

COMMENT ON COLUMN i2a.werkzaamheid.activiteit_id IS
    'DEPRECATED sinds 2026-09-02 — hield één willekeurige koppeling vast van de '
    'mediaan 356 die de RTR levert (gaps#G-136). Gebruik i2a.werkzaamheid_activiteit.';

-- Voortgang van de conservatieve ophaalronde, zodat afbreken gratis is.
CREATE TABLE IF NOT EXISTS i2a.werkzaamheid_koppel_run (
    werkzaamheid_urn text PRIMARY KEY
        REFERENCES i2a.werkzaamheid (urn) ON DELETE CASCADE,
    koppelingen      integer,
    paginas          integer,
    afgerond_op      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE i2a.werkzaamheid_koppel_run IS
    'Checkpoint per werkzaamheid. Een werkzaamheid die hierin staat wordt bij een '
    'volgende run overgeslagen, zodat de ophaalronde op elk moment afgebroken kan '
    'worden zonder werk te verliezen.';

COMMIT;
