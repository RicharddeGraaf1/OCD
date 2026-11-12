-- ============================================================================
-- 2026-11-12 · p2p.pons_historie — de pons van het omgevingsplan door de tijd.
--
-- WAAROM. p2p.pons kent alleen de pons van de geldende versie. Locus Legis wil in
-- het transitieplan laten zien wat er sinds 1-1-2024 is omgezet (tijdschuif in de
-- tab Gebieden). De DSO geeft de pons op een eerdere datum terug met geldigOp en
-- inWerkingOp, maar heeft geen lijst met versiedata. scripts/pons_historie.py
-- peilt daarom per maand en zoekt bij een wijziging de eerste dag op.
--
-- Eén rij per stap: vanaf geldig_vanaf geldt deze pons, tot de volgende rij van
-- dezelfde gemeente. De eerste rij is altijd 2024-01-01; geometrie is null als er
-- toen (of later, na intrekking) geen pons was.
--
-- Idempotent: alleen IF NOT EXISTS.
-- ============================================================================

CREATE TABLE IF NOT EXISTS p2p.pons_historie (
    gemeentecode            text        NOT NULL,
    geldig_vanaf            date        NOT NULL,
    geometrie_identificatie text        NULL,
    geometrie               geometry(MultiPolygon, 28992) NULL,
    expressie               text        NULL,   -- expressionId van het omgevingsplan op die dag
    gepeild_op              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gemeentecode, geldig_vanaf)
);

COMMENT ON TABLE p2p.pons_historie IS
    'Pons van het omgevingsplan per gemeente door de tijd (sinds 2024-01-01). Gevuld door dso-loader/scripts/pons_historie.py; gelezen door /v1/transitie/{gm}/ponshistorie.';
