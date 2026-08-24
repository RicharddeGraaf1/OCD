-- 2026-08-24: versie-historie (geldigheid) per regeling uit /voorkomens.
--
-- Achtergrond: p2p.regeling kent alleen de vigerende expression per frbr_work
-- (plus inactief-gemarkeerde verdrongen/ingetrokken rijen), maar niet van
-- wanneer tot wanneer elke versie gold. Presenteren v8 levert dat via
-- GET /regelingen/{id}/voorkomens: alle versies door de tijd — verleden,
-- heden én toekomstige (al geregistreerde) versies.
--
-- Modelkeuzes (onderbouwd 2026-08-24, zie vault-analysis
-- "Regelingversie-geldigheid via het voorkomens-endpoint"):
--   * eind_geldigheid EXPLICIET, niet afleiden uit de opvolger-begindatum:
--     bij intrekking eindigt het laatste voorkomen zonder opvolger
--     (bv. Voorbereidingsbesluit datacentra gm0546, eind 2026-04-16).
--   * geldigheids-as en inwerking-as zijn samengevoegd tot één kolompaar:
--     over 1.975 vigerende + 311 historische voorkomens was
--     beginGeldigheid == beginInwerking zonder uitzondering. De loader
--     bewaakt die aanname hard en faalt zodra Ozon ooit terugwerkende
--     kracht levert — dan pas komt er een aparte inwerking-kolom bij.
--   * registratie-as blijft apart: tijdstipRegistratie loopt maanden voor
--     op begin_geldigheid bij toekomstige versies.
--   * kennis-van-nu-snapshot: eind_geldigheid van het vigerende voorkomen
--     wordt al gevuld zodra een opvolger geregistreerd is; re-sync werkt
--     bestaande rijen dus bij (upsert) en verwijdert verdwenen voorkomens
--     (teruggetrokken toekomstige versies) per work.
--
-- Let op: geen FK naar p2p.regeling — historische expressions bestaan daar
-- per ontwerp niet.

CREATE TABLE IF NOT EXISTS p2p.regeling_voorkomen (
    frbr_expression      TEXT PRIMARY KEY,
    frbr_work            TEXT NOT NULL,
    versie               TEXT NULL,          -- niet-sequentieel, soms voorloopnul
    begin_geldigheid     DATE NOT NULL,      -- == beginInwerking; loader-guard bewaakt
    eind_geldigheid      DATE NULL,          -- NULL = (nog) onbeperkt; ook gevuld bij intrekking
    tijdstip_registratie TIMESTAMPTZ NOT NULL,
    eind_registratie     TIMESTAMPTZ NULL,
    publicatie_id        TEXT NULL,
    gesynct_op           TIMESTAMPTZ NOT NULL DEFAULT now(),
    geldig               DATERANGE GENERATED ALWAYS AS
                         (daterange(begin_geldigheid, eind_geldigheid, '[)')) STORED
);
CREATE INDEX IF NOT EXISTS idx_voorkomen_work
    ON p2p.regeling_voorkomen(frbr_work, begin_geldigheid);

COMMENT ON TABLE p2p.regeling_voorkomen IS
    'Versie-historie per regeling uit Presenteren v8 /voorkomens (kennis-van-nu). '
    '"Welke versie gold op X" = WHERE frbr_work = ? AND geldig @> ?::date. '
    'Geldigheid == inwerking zolang Ozon geen terugwerkende kracht levert; '
    'de loader faalt hard als die aanname breekt.';
