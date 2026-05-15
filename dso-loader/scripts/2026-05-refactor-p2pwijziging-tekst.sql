-- ============================================================================
-- 2026-05 · p2pwijziging.tekst_delta -> tekst_element (mirror van p2p)
--
-- Probe op /ontwerpregelingen en /besluitversies (zie chat 2026-05-15) liet
-- twee dingen zien:
--
-- 1. De huidige `_store_documentstructuur` zoekt naar
--    `_embedded.documentComponenten`, terwijl de DSO-API
--    `ontwerpDocumentComponenten` resp. `besluitversieDocumentComponenten`
--    levert. Resultaat: alle 214 wijzigingen kregen een lege `tekst_delta`
--    zonder dat er een exception viel.
--
-- 2. De renvooi-attributen op DocumentComponent-niveau zijn:
--      - wijzigactie ∈ {voegtoe, verwijder, nieuweContainer, verwijderContainer}
--      - vervallen   = true
--      - bevatRenvooi / bevatOntwerpInformatie = bool
--    De `_delta.bewerking`-aanname uit de oude loader was fout — die key
--    bestaat niet in de payload. De oude `bewerking`-ENUM
--    ('toevoegen','wijzigen','verwijderen') is een lossy mapping van de
--    echte STOP-renvooi-namen.
--
-- Daarom: tekst_delta vervalt, en p2pwijziging krijgt een volledige
-- tekst_element-mirror van p2p.tekst_element + renvooi-kolommen. Dat sluit
-- aan op wat de API echt levert (volle boom met markers op gewijzigde
-- nodes), maakt de viewer-symmetrie met p2p.tekst_element triviaal, en
-- voorkomt JOIN's met p2p om context van een lid te zien.
--
-- Voor regelingen die als geheel vervangen worden (geen
-- _links.beoogdeOpvolgerVan op het ontwerp resp. _links.wijzigtRegelingversie
-- op de besluitversie) staat een vlag op besluit, zodat de viewer "alles is
-- nieuw" weet zonder over alle nodes te hoeven lopen.
--
-- Veilig om te draaien: tekst_delta is leeg. Geen FK's wijzen ernaar.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-05-refactor-p2pwijziging-tekst.sql
-- ============================================================================

-- ── 1. Vlag op besluit voor vervangRegeling-stijl ─────────────────────
ALTER TABLE p2pwijziging.besluit
    ADD COLUMN IF NOT EXISTS is_vervang_regeling BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN p2pwijziging.besluit.is_vervang_regeling IS
    'TRUE als dit ontwerp/besluit de hele regeling vervangt i.p.v. een delta. '
    'Bron: afwezigheid van _links.beoogdeOpvolgerVan (ontwerp) of '
    '_links.wijzigtRegelingversie (besluit) in de Presenteren-API listing.';

-- ── 2. Drop oude tekst_delta ─────────────────────────────────────────
DROP TABLE IF EXISTS p2pwijziging.tekst_delta;

-- ── 3. Nieuwe tekst_element — mirror van p2p.tekst_element + renvooi ─
CREATE TABLE p2pwijziging.tekst_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id   TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    eid                 TEXT NOT NULL,
    wid                 TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    parent_id           BIGINT NULL
        REFERENCES p2pwijziging.tekst_element(id) ON DELETE CASCADE,
    nummer              TEXT NULL,
    opschrift           TEXT NULL,
    inhoud              TEXT NULL,
    inhoud_plain        TEXT GENERATED ALWAYS AS (
        regexp_replace(
            regexp_replace(COALESCE(inhoud, ''), '<[^>]+>', ' ', 'g'),
            '\s+', ' ', 'g'
        )
    ) STORED,
    volgorde            INT NOT NULL DEFAULT 0,
    -- Renvooi-attributen, NULL/FALSE = ongewijzigd t.o.v. de versie waarop
    -- dit ontwerp/besluit voortbouwt. Waardenset gemeten in de probe op
    -- 2026-05-15; CHECK is bewust laks (geen exhaustieve enum) voor het
    -- geval STOP er later iets bijgooit.
    wijzigactie         TEXT NULL CHECK (wijzigactie IN
        ('voegtoe', 'verwijder', 'nieuweContainer', 'verwijderContainer')),
    vervallen           BOOLEAN NOT NULL DEFAULT FALSE,
    bevat_renvooi       BOOLEAN NOT NULL DEFAULT FALSE,
    bevat_ontwerp_informatie BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (ontwerpbesluit_id, eid)
);

CREATE INDEX idx_pw_tekst_element_besluit
    ON p2pwijziging.tekst_element(ontwerpbesluit_id);
CREATE INDEX idx_pw_tekst_element_parent
    ON p2pwijziging.tekst_element(parent_id);
CREATE INDEX idx_pw_tekst_element_wid
    ON p2pwijziging.tekst_element(wid);
-- Filter-indexen voor de typische "toon mij wat er wijzigt"-query
CREATE INDEX idx_pw_tekst_element_wijzigactie
    ON p2pwijziging.tekst_element(ontwerpbesluit_id, wijzigactie)
    WHERE wijzigactie IS NOT NULL;
CREATE INDEX idx_pw_tekst_element_renvooi
    ON p2pwijziging.tekst_element(ontwerpbesluit_id)
    WHERE bevat_renvooi = TRUE;
-- FTS over de nieuwe tekst (inhoud na renvooi-verwerking)
CREATE INDEX idx_pw_tekst_element_inhoud_fts
    ON p2pwijziging.tekst_element
    USING gin (to_tsvector('dutch', coalesce(inhoud_plain, '')));

COMMENT ON TABLE p2pwijziging.tekst_element IS
    'Documentstructuur van een ontwerp/besluitversie als volle boom — niet '
    'sparse. Vervangt de oude tekst_delta. Renvooi-info zit in '
    'wijzigactie / vervallen / bevat_renvooi op de gewijzigde nodes; '
    'ongewijzigde nodes hebben die kolommen NULL/FALSE. Inline renvooi '
    '(NieuweTekst/VerwijderdeTekst, wijzigactie="..." op <Al>) blijft '
    'als XML in inhoud staan zodat de viewer het direct kan stylen.';
