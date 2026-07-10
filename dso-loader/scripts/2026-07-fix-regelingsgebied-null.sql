-- Datafix 2026-07-10: actieve expressies zonder regelingsgebied_id vallen
-- buiten élke geo-scope (semantisch.py scope-CTE joint op regelingsgebied →
-- locatie_subdiv). Trof exact 2 van 1939 actieve regelingen:
-- Omgevingsverordening Zeeland (@2026-06-01) en Omgevingsverordening
-- provincie Groningen — beide nieuwe expressies geladen zonder
-- regelingsgebied-koppeling. Loader-oorzaak (p2p-regeling-load pad) nog te
-- onderzoeken; dit script neemt het regelingsgebied van de meest recente
-- expressie van hetzelfde work over (ambtsgebied is versie-stabiel).
-- Idempotent: raakt alleen rijen met regelingsgebied_id IS NULL.

UPDATE p2p.regeling r
SET regelingsgebied_id = prev.regelingsgebied_id
FROM (
    SELECT DISTINCT ON (frbr_work) frbr_work, regelingsgebied_id
    FROM p2p.regeling
    WHERE regelingsgebied_id IS NOT NULL
    ORDER BY frbr_work, frbr_expression DESC
) prev
WHERE r.frbr_work = prev.frbr_work
  AND r.regelingsgebied_id IS NULL
  AND NOT r.inactief
RETURNING r.opschrift, r.frbr_expression, r.regelingsgebied_id;
