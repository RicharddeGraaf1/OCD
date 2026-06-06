-- ============================================================================
-- 2026-05 · is_niet_annoteerbaar op tekst_element
--
-- Bijlagen en toelichtingen mogen volgens de standaard NIET met OW-objecten
-- worden geannoteerd (gebruiker-bevestigd 2026-05-12). Tekst-elementen die
-- (transitief) onder een Bijlage/Toelichting-tak hangen, krijgen
-- `is_niet_annoteerbaar = TRUE` en worden in de stap-3-consistentie-view
-- afgevoerd naar een eigen klasse `niet_annoteerbaar` i.p.v. tot
-- false-positive te leiden in `vermoedelijk_vergeten_annotatie`.
--
-- Markering geldt voor element_type ∈ {Bijlage, Toelichting,
-- AlgemeneToelichting, ArtikelgewijzeToelichting} en alle nakomelingen.
--
-- Refresh-strategie: dit script kan na elke ingest opnieuw gedraaid worden
-- om nieuwe tekst_elements te markeren. Idempotent.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-niet-annoteerbaar.sql
-- ============================================================================

ALTER TABLE p2p.tekst_element
    ADD COLUMN IF NOT EXISTS is_niet_annoteerbaar BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_tekst_element_niet_annoteerbaar
    ON p2p.tekst_element (is_niet_annoteerbaar)
    WHERE is_niet_annoteerbaar = TRUE;

-- Recursive UPDATE: markeer alle tekst_elements die in een Bijlage- of
-- Toelichting-tak zitten. We starten bij de "wortels" van zulke takken
-- (element_type matcht) en lopen via parent_id naar boven om alle
-- nakomelingen te vinden — wacht, andersom: we lopen *omlaag* via
-- de inverse relatie (kinderen vinden hun parent via parent_id, dus
-- vanaf een wortel naar beneden gaan we via "find all te waar parent_id IN walk").
WITH RECURSIVE
walk AS (
    -- Begin: alle tekst_elements waarvan het type zelf niet-annoteerbaar is
    SELECT id
    FROM p2p.tekst_element
    WHERE element_type IN ('Bijlage', 'Toelichting',
                           'AlgemeneToelichting', 'ArtikelgewijzeToelichting')
    UNION
    -- Recursief: alle kinderen van iets dat in walk zit
    SELECT child.id
    FROM p2p.tekst_element child
    JOIN walk w ON child.parent_id = w.id
)
UPDATE p2p.tekst_element te
SET is_niet_annoteerbaar = TRUE
FROM walk
WHERE te.id = walk.id
  AND te.is_niet_annoteerbaar IS DISTINCT FROM TRUE;
