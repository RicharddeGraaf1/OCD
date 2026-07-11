-- 2026-07-11: voorberekende regel-op-locatie-keten voor /v1/locatie//v1/adres.
--
-- De runtime-query van _wat_geldt_hier joinde per aanroep de volledige
-- fan-out ala → jr → te → r (31k+ rijen per punt vóór dedup, ~550k
-- buffer-pages). Op productie (2GB memory-cap, netwerk-disk) kostte dat
-- 16-37s per call; ook koud op dev 8,5s. De keten is punt-ONafhankelijk —
-- alleen het ST_Intersects op locatie_subdiv is dat niet. Deze mv slaat de
-- gededupliceerde keten plat op (~379k compacte rijen, geen toast), zodat
-- runtime = punt-lookup + klein filter.
--
-- Refresh: na elke load-run (zelfde moment als de andere mv's, zie
-- refresh_drieslag.py / run_overnight). Kost ~3-5s.

-- Railway-containers hebben een kleine /dev/shm; parallelle builds crashen
-- daar op "could not resize shared memory segment" (zelfde valkuil als de
-- HNSW-restore). Sessie-lokaal, dus veilig ook voor dev.
SET max_parallel_workers_per_gather = 0;

DROP MATERIALIZED VIEW IF EXISTS p2p.mv_regel_op_locatie;

CREATE MATERIALIZED VIEW p2p.mv_regel_op_locatie AS
SELECT DISTINCT
       ala.locatie_id,
       te.id                                   AS te_id,
       r.opschrift                             AS regeling,
       r.documenttype,
       r.frbr_work,
       r.bronhouder,
       ocd_artikel_label(te.opschrift, te.wid) AS artikel,
       a.naam                                  AS a_naam,
       ala.kwalificatie
FROM p2p.activiteit_locatieaanduiding ala
JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
    AND (te.regeling_expression = jr.regeling_expression OR jr.regeling_expression IS NULL)
JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
LEFT JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
WHERE NOT r.inactief;

CREATE INDEX idx_mv_rol_locatie ON p2p.mv_regel_op_locatie (locatie_id);
CREATE INDEX idx_mv_rol_te ON p2p.mv_regel_op_locatie (te_id);
ANALYZE p2p.mv_regel_op_locatie;
