-- ============================================================================
-- 2026-07-21 · Materialiseer core.v_geo_health → core.mv_geo_health.
-- v_geo_health doet een DISTINCT over p2p.locatie_subdiv (~7M rijen, 12 GB) +
-- een anti-join tegen de UNION van 5 koppel-tabellen → ~26s, timeout op
-- /v1/data-health (statement_timeout 10s). Het is een monitoring-snapshot, geen
-- real-time data, dus een matview is de juiste vorm (analoog aan
-- core.mv_bronhouder_health). Refresh in de post-processing van full_sync.
-- Run: psql ... -f scripts/2026-07-21-mv-geo-health.sql
-- ============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS core.mv_geo_health AS
SELECT * FROM core.v_geo_health;
