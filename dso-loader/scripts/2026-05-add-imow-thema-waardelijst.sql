-- ============================================================================
-- 2026-05 · IMOW Thema-waardelijst v5.1.0
--
-- Voegt core.imow_thema toe en seedt 'm met de 28 waarden uit de IMOW-
-- waardelijst v5.1.0 (publicatiedatum 2026-01-21). Bevat 21 actieve en
-- 7 deprecated waarden — endpoint kan deprecated filteren of expliciet
-- als zodanig markeren.
--
-- `label` is de natural key: dat is de waarde zoals 'tekstdeel.thema' 'm
-- al bevat (lowercase, met spaties). `term` is de IMOW-PascalCase-vorm.
-- `uri` is informatief.
--
-- Idempotent: ON CONFLICT DO NOTHING.
-- Run:  docker exec -i dso-postgis psql -U postgres -d dso \
--          < scripts/2026-05-add-imow-thema-waardelijst.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS core.imow_thema (
    label       TEXT PRIMARY KEY,
    term        TEXT NOT NULL UNIQUE,
    deprecated  BOOLEAN NOT NULL DEFAULT FALSE,
    uri         TEXT NULL
);

CREATE INDEX IF NOT EXISTS imow_thema_deprecated_idx
    ON core.imow_thema (deprecated);

INSERT INTO core.imow_thema (label, term, deprecated, uri) VALUES
  ('bodem', 'Bodem', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Bodem'),
  ('bouwen', 'Bouwen', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Bouwen'),
  ('bouwwerken', 'Bouwwerken', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Bouwwerken'),
  ('cultureel erfgoed', 'CultureelErfgoed', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/CultureelErfgoed'),
  ('duurzaamheid', 'Duurzaamheid', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Duurzaamheid'),
  ('economie', 'Economie', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Economie'),
  ('energie', 'Energie', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Energie'),
  ('energie en natuurlijke hulpbronnen', 'EnergieEnNatuurlijkeHulpbronnen', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/EnergieEnNatuurlijkeHulpbronnen'),
  ('erfgoed', 'Erfgoed', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Erfgoed'),
  ('externe veiligheid', 'ExterneVeiligheid', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/ExterneVeiligheid'),
  ('geluid', 'Geluid', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Geluid'),
  ('gezondheid', 'Gezondheid', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Gezondheid'),
  ('infrastructuur', 'Infrastructuur', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Infrastructuur'),
  ('landbouw', 'Landbouw', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Landbouw'),
  ('landgebruik', 'Landgebruik', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Landgebruik'),
  ('landschap', 'Landschap', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Landschap'),
  ('lucht', 'Lucht', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Lucht'),
  ('milieu', 'Milieu', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Milieu'),
  ('milieu algemeen', 'MilieuAlgemeen', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/MilieuAlgemeen'),
  ('mobiliteit', 'Mobiliteit', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Mobiliteit'),
  ('natuur', 'Natuur', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Natuur'),
  ('planologisch gebruik', 'PlanologischGebruik', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/PlanologischGebruik'),
  ('procedures', 'Procedures', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Procedures'),
  ('recreatie', 'Recreatie', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Recreatie'),
  ('veiligheid', 'Veiligheid', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Veiligheid'),
  ('water', 'Water', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Water'),
  ('water en watersystemen', 'WaterEnWatersystemen', TRUE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/WaterEnWatersystemen'),
  ('wonen', 'Wonen', FALSE, 'http://standaarden.omgevingswet.overheid.nl/thema/id/concept/Wonen')
ON CONFLICT (label) DO NOTHING;

\echo '── IMOW Thema-waardelijst geladen ──'
SELECT
    COUNT(*)                            AS aantal,
    COUNT(*) FILTER (WHERE deprecated)  AS deprecated_count
FROM core.imow_thema;
