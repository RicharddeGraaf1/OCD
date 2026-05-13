-- ============================================================================
-- 2026-05 · Tekst-inline-referentie (drieslag tekst↔object — type 2)
--
-- Voegt p2p.tekst_inline_referentie toe: één rij per inline verwijzing
-- (IntIoRef / ExtIoRef / IntRef / ExtRef) binnen `tekst_element.inhoud`.
-- Tot nu toe zaten deze verwijzingen begraven in de XHTML-blob; hierna
-- queryable als platte tabel.
--
-- Achtergrond: dit is stap 1 van het plan in
--   vault_v1/analysis/Plan implementatie drieslag tekst-object.md
-- en sluit (deels) gap G-64. Type 1 (annotatie) zit al in p2p; type 3
-- (naam-match) komt in stap 2 als materialized view in dso.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-add-tekst-inline-referentie.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS p2p.tekst_inline_referentie (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tekst_element_id   BIGINT NOT NULL REFERENCES p2p.tekst_element(id) ON DELETE CASCADE,
    soort              TEXT   NOT NULL CHECK (soort IN ('IntIoRef','ExtIoRef','IntRef','ExtRef')),
    target_ref         TEXT   NOT NULL,                 -- @ref: eId-pad (IntIoRef→ExtIoRef.wId), FRBR-expression (ExtIoRef→GIO), eId (IntRef), URL (ExtRef)
    eigen_wid          TEXT   NULL,                     -- alleen voor ExtIoRef: de @wId waarmee IntIoRef'en uit zelfde regeling hier naartoe wijzen
    target_soort       TEXT   NULL CHECK (target_soort IS NULL OR target_soort IN ('GIO','Bijlage','Tekstcomponent','Extern')),
    target_gio_expression TEXT NULL REFERENCES p2p.geo_informatieobject(frbr_expression) ON DELETE SET NULL,
                                                        -- ingevuld voor ExtIoRef bij FRBR-match; voor IntIoRef via twee-traps lookup op eigen_wid
    positie            INTEGER NULL,                    -- offset binnen `inhoud` voor highlighting
    UNIQUE (tekst_element_id, soort, target_ref, positie)
);

-- ALTER vóór de indices: als de tabel uit een eerdere migratie nog de oude
-- kolomset heeft, voeg eigen_wid eerst toe zodat het index-statement hieronder
-- niet faalt. Idempotent via IF NOT EXISTS.
ALTER TABLE p2p.tekst_inline_referentie
    ADD COLUMN IF NOT EXISTS eigen_wid TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_tekst_inline_ref_element
    ON p2p.tekst_inline_referentie (tekst_element_id);
CREATE INDEX IF NOT EXISTS idx_tekst_inline_ref_target
    ON p2p.tekst_inline_referentie (target_ref);
CREATE INDEX IF NOT EXISTS idx_tekst_inline_ref_gio
    ON p2p.tekst_inline_referentie (target_gio_expression)
    WHERE target_gio_expression IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tekst_inline_ref_soort
    ON p2p.tekst_inline_referentie (soort);
CREATE INDEX IF NOT EXISTS idx_tekst_inline_ref_eigen_wid
    ON p2p.tekst_inline_referentie (eigen_wid)
    WHERE eigen_wid IS NOT NULL;

COMMENT ON TABLE  p2p.tekst_inline_referentie IS
    'Inline tekstuele verwijzingen (IntIoRef/ExtIoRef/IntRef/ExtRef) per tekst_element. '
    'Type 2 in de drieslag tekst↔object — zie vault_v1 wiki.';
COMMENT ON COLUMN p2p.tekst_inline_referentie.soort IS
    'STOP-elementtype. IntIoRef = intern naar Informatieobject (GIO/Bijlage); '
    'ExtIoRef = extern naar Informatieobject; IntRef = intern naar tekstcomponent; '
    'ExtRef = externe URL.';
COMMENT ON COLUMN p2p.tekst_inline_referentie.target_soort IS
    'Afgeleid bij of na ingest. Voor IntIoRef: ''GIO'' als ref matcht met '
    'p2p.geo_informatieobject.frbr_expression, anders ''Bijlage'' of ''Tekstcomponent''.';
