-- ============================================================================
-- 2026-06 · vth.dossier_doorlooptijd — doorlooptijd per vergunning-dossier
--
-- Aanleiding (gebruiker, 2026-06-24): publiek doorlooptijd-dashboard op
-- omgevingsvergunningenregister.nl, in de stijl van annotatieconformiteit.nl.
-- Onderbouwing: vault_v1/analysis/Doorlooptijd-dashboard omgevingsvergunningen.md
-- (concretisering van item B1 levenscyclus-clustering uit
--  vault_v1/analysis/Vergunningkennisgeving - verrijking data en voorziening.md).
--
-- Doorlooptijd = datum_besluit − datum_aanvraag (publicatiedatums van de
-- kennisgevingen). Een dossier koppelt een aanvraag aan een TERMINAAL besluit;
-- de uitkomst staat in de kolom `uitkomst`:
--     verleend | geweigerd | van_rechtswege | ingetrokken
-- (de eerst-gepubliceerde terminale gebeurtenis sluit het dossier).
--
-- TWEE koppelmethodes, kolom `match_methode`:
--   'zaaknummer' (exact): aanvraag + terminaal op gelijk (bg_naam, zaaknummer_bg).
--   'adres' (benadering): voor verleningen/besluiten zónder zaaknummer-koppeling,
--       match op gelijk (bg_naam, adres, activiteit_code) met een aanvraag ≤365
--       dagen eerder; ÉÉN-OP-ÉÉN greedy (elke aanvraag én elk besluit hooguit één
--       keer, kleinste tijdkloof wint) → geen dubbeltelling op druk bezet adres.
--
-- Filtering: het endpoint /v1/vergunningen/doorlooptijd kan op `uitkomst` en
-- `match_methode` filteren; de matview houdt alle varianten vast.
--
-- CAVEATS (methodologie-noot dashboard): kennisgevings-doorlooptijd ≠ wettelijke
-- beslistermijn (ontvangst→besluit); selectiebias richting consistent publicerende
-- BG (adres-trap verkleint dit); adres is deels uit titel geparset (vrije tekst).
--
-- Refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;
--   (vereist de unieke index op dossier_key). Draaien NA elke koop-ingest/-refresh.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-dossier-doorlooptijd.sql
--
-- Structuurwijziging (kolommen match_methode/uitkomst/dossier_key); DROP + CREATE.
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS vth.dossier_doorlooptijd;

CREATE MATERIALIZED VIEW vth.dossier_doorlooptijd AS
WITH src AS (
    SELECT
        koop_id, type_besluit, bg_naam, datum_publicatie, zaaknummer_bg,
        activiteit_code, organisatietype, ligt_in_gemeente, afwijk_status,
        lower(
            coalesce(nullif(trim(postcode), ''), '')     || '|' ||
            coalesce(nullif(trim(huisnummer), ''), '')   || '|' ||
            coalesce(nullif(trim(straatnaam), ''), '')   || '|' ||
            coalesce(nullif(trim(woonplaats), ''), '')
        ) AS addr,
        (nullif(trim(huisnummer), '') IS NOT NULL
         AND (nullif(trim(postcode), '') IS NOT NULL
              OR nullif(trim(straatnaam), '') IS NOT NULL)) AS has_addr
    FROM vth.vergunningkennisgeving
    -- aanvraag + alle terminale uitkomsten
    WHERE type_besluit IN ('aanvraag', 'verleend', 'geweigerd', 'van_rechtswege', 'ingetrokken')
),
-- (bg_naam, zaaknummer_bg) met een aanvraag én minstens één terminaal besluit
zk_pairs AS (
    SELECT bg_naam, zaaknummer_bg
    FROM src
    WHERE zaaknummer_bg IS NOT NULL
    GROUP BY 1, 2
    HAVING count(*) FILTER (WHERE type_besluit = 'aanvraag') > 0
       AND count(*) FILTER (WHERE type_besluit <> 'aanvraag') > 0
),
-- ── Trap 1: exacte zaaknummer-koppeling ──────────────────────────────
-- aanvraagdatum per dossier (+ activiteit-fallback)
zk_aanvraag AS (
    SELECT bg_naam, zaaknummer_bg,
           min(datum_publicatie) AS datum_aanvraag,
           min(activiteit_code)  AS activiteit_aanvraag
    FROM src
    WHERE type_besluit = 'aanvraag' AND zaaknummer_bg IS NOT NULL
    GROUP BY 1, 2
),
-- eerst-gepubliceerde terminale gebeurtenis per dossier (sluit het dossier)
zk_besluit AS (
    SELECT DISTINCT ON (bg_naam, zaaknummer_bg)
        bg_naam, zaaknummer_bg,
        datum_publicatie AS datum_besluit,
        type_besluit     AS uitkomst,
        activiteit_code, organisatietype, ligt_in_gemeente
    FROM src
    WHERE type_besluit <> 'aanvraag' AND zaaknummer_bg IS NOT NULL
    ORDER BY bg_naam, zaaknummer_bg, datum_publicatie ASC
),
-- BOPA-vlag per zaak: het hele dossier is een afwijkvergunning zodra één van de
-- kennisgevingen (aanvraag of besluit) buitenplans is (afwijk_status). Zie G-84 —
-- tekst-signaal, dus dit is een ondergrens.
zk_afwijk AS (
    SELECT bg_naam, zaaknummer_bg,
           bool_or(afwijk_status = 'buitenplans_expliciet') AS is_afwijk
    FROM src
    WHERE zaaknummer_bg IS NOT NULL
    GROUP BY 1, 2
),
tier1 AS (
    SELECT
        'zk:' || b.bg_naam || '|' || b.zaaknummer_bg AS dossier_key,
        'zaaknummer'::text                           AS match_methode,
        b.uitkomst,
        b.bg_naam,
        b.zaaknummer_bg,
        a.datum_aanvraag,
        b.datum_besluit,
        coalesce(b.activiteit_code, a.activiteit_aanvraag) AS activiteit_code,
        b.organisatietype,
        b.ligt_in_gemeente,
        coalesce(af.is_afwijk, false)                AS is_afwijk
    FROM zk_besluit b
    JOIN zk_aanvraag a USING (bg_naam, zaaknummer_bg)
    JOIN zk_pairs   p USING (bg_naam, zaaknummer_bg)
    JOIN zk_afwijk  af USING (bg_naam, zaaknummer_bg)
),
-- records die door trap 1 zijn opgebruikt (sluiten we uit in trap 2)
claimed AS (
    SELECT s.koop_id
    FROM src s
    JOIN zk_pairs p ON p.bg_naam = s.bg_naam AND p.zaaknummer_bg = s.zaaknummer_bg
),
free AS (
    SELECT * FROM src
    WHERE has_addr
      AND koop_id NOT IN (SELECT koop_id FROM claimed)
),
-- ── Trap 2: adres + activiteit, ÉÉN-OP-ÉÉN koppeling ─────────────────
cand AS (
    SELECT
        v.koop_id          AS v_id,
        a.koop_id          AS a_id,
        v.type_besluit     AS uitkomst,
        v.bg_naam,
        v.activiteit_code,
        v.organisatietype,
        v.ligt_in_gemeente,
        a.datum_publicatie AS datum_aanvraag,
        v.datum_publicatie AS datum_besluit,
        coalesce(v.afwijk_status = 'buitenplans_expliciet'
                 OR a.afwijk_status = 'buitenplans_expliciet', false) AS is_afwijk,
        (v.datum_publicatie - a.datum_publicatie) AS gap
    FROM free v
    JOIN free a
      ON a.type_besluit = 'aanvraag'
     AND a.bg_naam = v.bg_naam
     AND a.addr = v.addr
     AND coalesce(a.activiteit_code, '') = coalesce(v.activiteit_code, '')
     AND a.datum_publicatie <= v.datum_publicatie
     AND a.datum_publicatie >= v.datum_publicatie - 365
    WHERE v.type_besluit <> 'aanvraag'   -- elk terminaal besluit
),
-- pas 1: per besluit de dichtstbijzijnde aanvraag
v_best AS (
    SELECT * FROM (
        SELECT *, row_number() OVER (PARTITION BY v_id ORDER BY gap ASC, a_id) AS rn
        FROM cand
    ) s WHERE rn = 1
),
-- pas 2: per aanvraag wint het besluit met de kleinste kloof; rest valt af
tier2 AS (
    SELECT
        'adr:' || v_id  AS dossier_key,
        'adres'::text   AS match_methode,
        uitkomst,
        bg_naam,
        NULL::text      AS zaaknummer_bg,
        datum_aanvraag,
        datum_besluit,
        activiteit_code,
        organisatietype,
        ligt_in_gemeente,
        is_afwijk
    FROM (
        SELECT *, row_number() OVER (PARTITION BY a_id ORDER BY gap ASC, v_id) AS rn2
        FROM v_best
    ) s WHERE rn2 = 1
),
combined AS (
    SELECT * FROM tier1
    UNION ALL
    SELECT * FROM tier2
)
SELECT
    dossier_key,
    match_methode,
    uitkomst,
    bg_naam,
    zaaknummer_bg,
    datum_aanvraag,
    datum_besluit,
    (datum_besluit - datum_aanvraag)               AS doorlooptijd_dagen,
    activiteit_code,
    organisatietype,
    ligt_in_gemeente,
    is_afwijk,
    (date_trunc('quarter', datum_besluit))::date   AS kwartaal
FROM combined
WHERE datum_aanvraag IS NOT NULL
  AND datum_besluit IS NOT NULL
  AND datum_besluit >= datum_aanvraag;

-- Unieke index op de synthetische sleutel: nodig voor REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX idx_dossier_doorlooptijd_pk
    ON vth.dossier_doorlooptijd (dossier_key);
CREATE INDEX idx_dossier_doorlooptijd_bg
    ON vth.dossier_doorlooptijd (bg_naam);
CREATE INDEX idx_dossier_doorlooptijd_activiteit
    ON vth.dossier_doorlooptijd (activiteit_code);
CREATE INDEX idx_dossier_doorlooptijd_kwartaal
    ON vth.dossier_doorlooptijd (kwartaal);
CREATE INDEX idx_dossier_doorlooptijd_methode
    ON vth.dossier_doorlooptijd (match_methode);
CREATE INDEX idx_dossier_doorlooptijd_uitkomst
    ON vth.dossier_doorlooptijd (uitkomst);
CREATE INDEX idx_dossier_doorlooptijd_afwijk
    ON vth.dossier_doorlooptijd (is_afwijk);

COMMENT ON MATERIALIZED VIEW vth.dossier_doorlooptijd IS
    'Doorlooptijd per vergunning-dossier (aanvraag->terminaal besluit). '
    'uitkomst = verleend|geweigerd|van_rechtswege|ingetrokken; match_methode = '
    'zaaknummer (exact) of adres (benadering, één-op-één). Voedt '
    '/v1/vergunningen/doorlooptijd (filterbaar op uitkomst en methode). '
    'Kennisgevings-doorlooptijd, niet de officiele beslistermijn. '
    'Refresh CONCURRENTLY na elke koop-ingest.';
