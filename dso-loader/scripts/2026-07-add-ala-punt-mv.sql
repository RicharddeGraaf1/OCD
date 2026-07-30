-- ============================================================================
-- 2026-07 · p2p.ala_punt — gematerialiseerde activiteit-op-locatie
--
-- Aanleiding: elke punt-vraag in de viewer ("welke activiteiten gelden hier?")
-- leidde het antwoord live af uit een keten die enorm uitwaaiert. Gemeten op
-- Amsterdam (121687/487316):
--
--     40 locatie_subdiv-rijen op het punt
--       -> 13.763 activiteit_locatieaanduiding-rijen
--         -> 13.763 index-lookups op juridische_regel
--           -> 13.763 index-lookups op tekst_element (675.530 rijen)
--             -> 563 activiteiten als antwoord
--
-- Kosten daarvan op prod: 297.204 shared buffers (~2,3 GB) voor één klik,
-- tegen shared_buffers = 512 MB op een database van 55 GB. Daardoor bleef de
-- cache nooit warm (hit ratio 77,6%) en kostte de eerste klik in een nieuw
-- gebied tot 4,6 s, terwijl de tweede 0,36 s was.
--
-- Die uitwaaiering is pure herhaling: dezelfde activiteit hangt via tientallen
-- artikelen aan hetzelfde ambtsgebied. Voor de vraag "geldt dit hier?" is één
-- keer genoeg; welk artikel het precies is zoek je pas op als iemand doorklikt.
-- 383.793 ALA-rijen collabeen zo tot 70.475 unieke tupels (18,3%).
--
-- Gemeten effect van deze matview (prototype, identiek resultaat op
-- Amsterdam/Utrecht/Zaanstad — 564/611/544 activiteiten):
--
--     punt-query   165 ms -> 5 ms
--     buffers      146.137 -> 484
--
-- ── Twee ontwerpkeuzes, expliciet ──
--
-- 1. `regeling_expression` komt uit `juridische_regel` (99,942% gevuld: 160
--    van 276.677 regels leeg). Voor die 160 valt tak B terug op
--    `tekst_element`. Daar geldt de bekende wId-fan-out: `te.wid` is NIET
--    uniek over regelingen heen. We pakken daarom NIET willekeurig één
--    treffer (dat plakt de activiteit aan een mogelijk verkeerde regeling),
--    maar nemen ze allemaal mee — precies wat de live-join nu ook doet.
--
-- 2. GEEN inactief-filter hierin. Of een regeling verdrongen is verandert
--    zonder dat er een ALA wijzigt; dat in de matview bakken zou een tweede
--    soort veroudering introduceren. De endpoints joinen zelf op p2p.regeling
--    (klein, en vanaf hier nog maar enkele honderden expressions).
--
-- Refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.ala_punt;
--   (vereist de unieke index hieronder). Opgenomen in refresh_drieslag.py.
--   Draaien na elke ingest/backfill die p2p.activiteit_locatieaanduiding of
--   p2p.juridische_regel raakt. Bouwtijd ~1,5 s, omvang ~16 MB incl. indices.
--
-- Idempotent: IF NOT EXISTS op matview en indices.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-07-add-ala-punt-mv.sql
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS p2p.ala_punt AS
-- Tak A: regeling bekend op de juridische regel zelf (99,942%).
SELECT DISTINCT
    ala.locatie_id,
    ala.activiteit_id,
    ala.kwalificatie,
    jr.regeling_expression
FROM p2p.activiteit_locatieaanduiding ala
JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
WHERE jr.regeling_expression IS NOT NULL

UNION

-- Tak B: de 160 regels zonder regeling_expression, via tekst_element. Levert
-- meerdere rijen als de wId in meerdere regelingen voorkomt (zie keuze 1).
SELECT DISTINCT
    ala.locatie_id,
    ala.activiteit_id,
    ala.kwalificatie,
    te.regeling_expression
FROM p2p.activiteit_locatieaanduiding ala
JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
WHERE jr.regeling_expression IS NULL
  AND te.regeling_expression IS NOT NULL;

-- Unieke index: nodig voor REFRESH ... CONCURRENTLY. NULLS NOT DISTINCT omdat
-- activiteit_id en kwalificatie nullable zijn en twee rijen die alleen in een
-- NULL verschillen hier hetzelfde feit zijn (zelfde conventie als
-- uq_ala_natural op de brontabel).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ala_punt_pk
    ON p2p.ala_punt (locatie_id, activiteit_id, kwalificatie, regeling_expression)
    NULLS NOT DISTINCT;

-- De toegangsweg van elke punt-query: locatie_subdiv (GIST) -> hierheen.
CREATE INDEX IF NOT EXISTS idx_ala_punt_locatie
    ON p2p.ala_punt (locatie_id);

-- Voor de omgekeerde vraag: welke locaties horen bij deze regeling?
CREATE INDEX IF NOT EXISTS idx_ala_punt_regeling
    ON p2p.ala_punt (regeling_expression);

COMMENT ON MATERIALIZED VIEW p2p.ala_punt IS
    'Distinct (locatie_id, activiteit_id, kwalificatie, regeling_expression) '
    'uit activiteit_locatieaanduiding x juridische_regel. Vervangt de live '
    'jr/tekst_element-keten in de punt-endpoints (viewer_objecten, viewer_ala, '
    'viewer_regelingen, viewer_regelmix): 383.793 ALA-rijen -> ~70k tupels, '
    'punt-query 165ms -> 5ms. Bevat GEEN inactief-filter; dat doet de caller.';
