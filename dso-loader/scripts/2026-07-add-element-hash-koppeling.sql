-- Koppeltabel functioneel id ↔ content-hash voor hertalingen (2026-07-22).
--
-- v2a.hertaling is content-adresseerbaar (bron_hash = v2a.norm_hash(inhoud_plain))
-- zodat één hertaling alle bruidsschat-duplicaten dekt. Maar afnemers willen
-- niet op hashes matchen: zij kennen het functionele id (wid + expressie, of
-- tekst_element.id). Deze MV materialiseert element → hash; de view
-- v2a.element_hertaling joint LIVE met v2a.hertaling zodat nieuwe golfjes
-- direct zichtbaar zijn zonder refresh.
--
-- Ontwerpkeuzes:
--  * p2p blijft onaangeroerd (bron-getrouwe laag); afgeleiden in v2a.
--  * wid is NIET uniek over expressies (wId-fanout, 2026-06) — daarom draagt
--    de MV ook regeling_expression en is tekst_element_id de unieke sleutel.
--  * element→hash verandert alleen bij regeling-reload → REFRESH via
--    refresh_drieslag.py; de hertaling-join is live (geen stale MV bij golfjes).
--
-- Refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY v2a.mv_element_hash
-- (opgenomen in scripts/refresh_drieslag.py).

CREATE MATERIALIZED VIEW IF NOT EXISTS v2a.mv_element_hash AS
SELECT te.id AS tekst_element_id,
       te.wid,
       te.regeling_expression,
       te.element_type,
       v2a.norm_hash(te.inhoud_plain) AS bron_hash
FROM p2p.tekst_element te
WHERE te.element_type IN ('Artikel', 'Lid', 'Divisietekst')
  AND te.inhoud_plain IS NOT NULL
  AND length(te.inhoud_plain) > 30;

-- Unique index vereist voor REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS mv_element_hash_te_id
    ON v2a.mv_element_hash (tekst_element_id);
CREATE INDEX IF NOT EXISTS mv_element_hash_wid
    ON v2a.mv_element_hash (wid);
CREATE INDEX IF NOT EXISTS mv_element_hash_bron_hash
    ON v2a.mv_element_hash (bron_hash);
CREATE INDEX IF NOT EXISTS mv_element_hash_expr
    ON v2a.mv_element_hash (regeling_expression);

-- Gemaks-view: functioneel id → hertaling, zonder hash-kennis bij de afnemer.
-- Eén rij per (element, model, prompt_versie); afnemers filteren op model.
CREATE OR REPLACE VIEW v2a.element_hertaling AS
SELECT mh.tekst_element_id,
       mh.wid,
       mh.regeling_expression,
       mh.element_type,
       mh.bron_hash,
       h.model,
       h.prompt_versie,
       h.tekst  AS begrijpelijk,
       h.status,
       h.gegenereerd_op
FROM v2a.mv_element_hash mh
JOIN v2a.hertaling h ON h.bron_hash = mh.bron_hash;

COMMENT ON MATERIALIZED VIEW v2a.mv_element_hash IS
  'Koppeling tekst_element → content-hash (v2a.norm_hash van inhoud_plain). '
  'Refresh bij regeling-reload via refresh_drieslag.py.';
COMMENT ON VIEW v2a.element_hertaling IS
  'Functioneel id (wid+expressie / tekst_element_id) → begrijpelijke hertaling. '
  'Live join: nieuwe hertaling-golfjes direct zichtbaar. Filter op model+prompt_versie.';
