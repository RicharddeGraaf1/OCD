-- 2026-09-18 — omvang per regeling vooraf uitgerekend + index op pons_status
--
-- Aanleiding: /v1/register/landelijk (landelijk beeld op
-- omgevingsdocumentenregister.nl) gaf 500. De telling "grootste
-- omgevingsdocument" liep over alle tekst_elementen en kostte op productie
-- 39,8 s, ruim boven de statement-timeout van 20 s. Gemeten per onderdeel:
--   grootste omgevingsdocument  39,8 s   -> deze MV
--   aantal Wro-plannen (actief)   4,2 s   -> index hieronder + WHERE i.p.v. FILTER
--   overige onderdelen           < 1,2 s
--
-- De MV is breder bruikbaar dan landelijk beeld: omvang per regeling is een
-- vraag die vaker terugkomt (bronhouderprofiel, documentdetail).
--
-- Verversen: hoort bij de health-MV's, dus
--   python scripts/refresh_health_mvs.py --target prod
-- (staat in HEALTH_MVS). CONCURRENTLY kan dankzij de unieke index.

CREATE MATERIALIZED VIEW IF NOT EXISTS p2p.mv_regeling_omvang AS
SELECT r.frbr_expression,
       r.opschrift,
       r.documenttype,
       r.bronhouder,
       count(te.id) AS n_tekstelementen
FROM p2p.regeling r
LEFT JOIN p2p.tekst_element te ON te.regeling_expression = r.frbr_expression
WHERE NOT r.inactief
GROUP BY r.frbr_expression, r.opschrift, r.documenttype, r.bronhouder;

CREATE UNIQUE INDEX IF NOT EXISTS mv_regeling_omvang_pk
    ON p2p.mv_regeling_omvang (frbr_expression);
CREATE INDEX IF NOT EXISTS mv_regeling_omvang_n
    ON p2p.mv_regeling_omvang (n_tekstelementen DESC);

-- Wro: de telling `count(*) FILTER (WHERE pons_status = 'actief')` scant de hele
-- tabel. Met deze index en een gewone WHERE wordt het een index-scan.
CREATE INDEX IF NOT EXISTS idx_ruimtelijk_instrument_pons_status
    ON wro.ruimtelijk_instrument (pons_status);

ANALYZE p2p.mv_regeling_omvang;
ANALYZE wro.ruimtelijk_instrument;
