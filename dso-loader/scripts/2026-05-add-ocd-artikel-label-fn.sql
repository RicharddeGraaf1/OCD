-- ocd_artikel_label(opschrift, wid) → de menselijk-leesbare artikel-naam.
--
-- Achtergrond: `p2p.tekst_element.opschrift` is vaak NULL voor paragraph-level
-- elementen (`art_3.12__para_3` heeft geen eigen titel; alleen de art-node
-- daarboven heeft opschrift "Artikel 3.12 — Norm wateroverlast"). De join
-- `tekst_element ON wid = jr.regeltekst_wid` koppelt vaak naar het paragraph-
-- niveau waardoor `artikel` leeg terugkomt — terwijl de wid wel
-- `__art_<X.Y>__` bevat als structurele referentie.
--
-- Deze functie geeft:
--   - opschrift als die niet leeg is (R01-stijl: "Beoordelingsregel hoofdgebouw - bouwhoogte")
--   - anders 'Artikel <X.Y>' geparst uit de wid (R31-stijl: "Artikel 3.12")
--   - anders NULL (geen wid, geen opschrift)
--
-- Gebruik in queries:
--   SELECT ocd_artikel_label(te.opschrift, te.wid) AS artikel
--   FROM   p2p.tekst_element te ...
--
-- IMMUTABLE: zelfde input → zelfde output, geen DB-state nodig. Planner kan
-- 'm inlinen en hergebruiken in WHERE/ORDER BY zonder herevaluatie.

CREATE OR REPLACE FUNCTION ocd_artikel_label(opschrift text, wid text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $$
    SELECT COALESCE(
        NULLIF(opschrift, ''),
        'Artikel ' || substring(wid from 'art_([^_]+)')
    );
$$;

COMMENT ON FUNCTION ocd_artikel_label(text, text) IS
    'Menselijk-leesbare artikel-naam: opschrift indien gevuld, anders ''Artikel X.Y'' geparst uit wid. Zie scripts/2026-05-add-ocd-artikel-label-fn.sql.';
