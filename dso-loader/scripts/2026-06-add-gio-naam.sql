-- ============================================================================
-- 2026-06 · geo:naam-kolom op p2p.geo_informatieobject
--
-- Aanleiding (gebruiker, 2026-06-09): GIO's moeten als volwaardig object in de
-- objectlijsten (/v1/viewer/objecten en /v1/objecten) kunnen verschijnen. Tot nu
-- toe heeft een GIO in de DB alleen een FRBR-expression — geen leesbare naam.
-- De GIO-GML bevat `<geo:naam>` direct onder `geo:GeoInformatieObjectVersie`
-- (geverifieerd op Gemeentestad-voorbeeldbestanden, schemaversie 1.3.0).
--
-- Deze migratie voegt de kolom toe; de loader (gio_zip.py) en het backfill-script
-- (scripts/backfill_gio_naam.py) vullen hem.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-06-add-gio-naam.sql
-- ============================================================================

ALTER TABLE p2p.geo_informatieobject
    ADD COLUMN IF NOT EXISTS naam TEXT;

COMMENT ON COLUMN p2p.geo_informatieobject.naam IS
    'geo:naam uit de GIO-GML (geo:GeoInformatieObjectVersie/geo:naam). '
    'Leesbare titel; conventioneel gelijk aan de naam van de Gebiedsaanwijzing '
    'die op dit GIO zit (TPOD-OP §7.9). Gevuld door gio_zip.py + backfill_gio_naam.py.';
