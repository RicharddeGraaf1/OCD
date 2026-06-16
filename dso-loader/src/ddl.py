"""DDL statements for the DSO database schema.

Based on analysis/Datamodel v1.0 DDL.md in the vault.
Scope: ABCE, snapshot-only, read-only, all bronhouders.

Keten-gedreven schema-indeling (zie OCD/SCHEMA-INDELING.md):
  core — referentiegegevens (waardelijsten, bronhouder)
  p2p  — plan-tot-publicatie (STOP + CIM-OW, Ow-regime)
  wro  — oud regime (Wro/IMRO, sunset 2032)
  i2a  — idee-tot-afhandeling (IMTR, werkzaamheden)
  v2a  — vraag-tot-antwoord (gereserveerd; nu leeg)
"""

DDL = """
-- =============================================================
-- DSO Datamodel v1.0 — Postgres + PostGIS
-- Scope: ABCE (Ow + IMTR + Wro + waardelijsten)
-- Keten-gedreven schema-indeling: core / p2p / wro / i2a / v2a
-- =============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS p2p;
CREATE SCHEMA IF NOT EXISTS wro;
CREATE SCHEMA IF NOT EXISTS i2a;
CREATE SCHEMA IF NOT EXISTS v2a;

COMMENT ON SCHEMA core IS 'Referentiegegevens: waardelijsten, bronhouder';
COMMENT ON SCHEMA p2p IS 'Plan-tot-publicatie (Ow): STOP-regelingen, besluiten, CIM-OW objecten';
COMMENT ON SCHEMA wro IS 'Oud regime: Wro/IMRO bestemmingsplannen (sunset 2032)';
COMMENT ON SCHEMA i2a IS 'Idee-tot-afhandeling: IMTR toepasbare regels, werkzaamheden';
COMMENT ON SCHEMA v2a IS 'Vraag-tot-antwoord: viewer-data, vergunningen (gereserveerd)';

SET search_path TO p2p, wro, i2a, v2a, core, public;

-- =============================================================
-- core.* — Lookup-tabellen en stamgegevens
-- =============================================================

CREATE TABLE IF NOT EXISTS core.bestemmingshoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.dubbelbestemmingshoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.bouwaanduidingtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.maatvoeringsaanduiding (
    code TEXT PRIMARY KEY,
    eenheid TEXT NULL
);

CREATE TABLE IF NOT EXISTS core.figuurtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.gebiedsaanduidinghoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.dossierstatus (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.planstatus (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.regelingmodel (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.besluitmodel (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.publicatiebladtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.idealisatie (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.toestemmingstype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS core.documenttype (
    code TEXT PRIMARY KEY
);

-- IMOW Thema-waardelijst (versie 5.1.0). 28 waarden, 7 deprecated.
-- `term` is de IMOW-term (PascalCase), `label` is de presentatie-vorm
-- (lowercase met spaties) zoals die ook in `tekstdeel.thema` opgeslagen
-- staat — de label is de natural key voor JOIN's.
-- Bron: vault_v1/raw/valuelists/imow/5.1.0/waardelijsten IMOW 5.1.0.json
CREATE TABLE IF NOT EXISTS core.imow_thema (
    label       TEXT PRIMARY KEY,            -- 'bouwen', 'erfgoed', …
    term        TEXT NOT NULL UNIQUE,        -- 'Bouwen', 'Erfgoed', …
    deprecated  BOOLEAN NOT NULL DEFAULT FALSE,
    uri         TEXT NULL                    -- volledige IMOW-URI (informatief)
);
CREATE INDEX IF NOT EXISTS imow_thema_deprecated_idx
    ON core.imow_thema (deprecated);

-- Side-mapping voor hoofdlijn-soort. De IMOW-XML levert vrij-tekst soorten
-- (47+ varianten met case-verschillen, '-'-placeholder en ad-hoc beleids-
-- teksten als soort). Deze mapping rust raw → canonical zodat filters in
-- de viewer met een schone taxonomie werken; raw blijft op p2p.hoofdlijn
-- staan voor auditability. `reviewed=FALSE` markeert auto-gegenereerde
-- (lowercase + trim) entries die nog door een mens bekeken moeten worden.
CREATE TABLE IF NOT EXISTS core.hoofdlijn_soort_mapping (
    raw_value   TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL,
    reviewed    BOOLEAN NOT NULL DEFAULT FALSE,
    notitie     TEXT NULL
);
CREATE INDEX IF NOT EXISTS hoofdlijn_soort_mapping_canonical_idx
    ON core.hoofdlijn_soort_mapping (canonical);

CREATE TABLE IF NOT EXISTS core.waardelijst (
    uri             TEXT PRIMARY KEY,
    waardelijst     TEXT NOT NULL,
    label           TEXT NOT NULL,
    beschrijving    TEXT NULL,
    geldig_vanaf    DATE NULL,
    geldig_tot      DATE NULL
);

CREATE TABLE IF NOT EXISTS core.bronhouder (
    overheidscode   TEXT PRIMARY KEY,
    naam            TEXT NOT NULL,
    label           TEXT NULL,
    oin             TEXT NULL,
    bestuurslaag    TEXT NULL,
    ow_geladen      BOOLEAN NOT NULL DEFAULT FALSE,
    imtr_geladen    BOOLEAN NOT NULL DEFAULT FALSE,
    wro_geladen     BOOLEAN NOT NULL DEFAULT FALSE,
    wro_teksten_geladen BOOLEAN NOT NULL DEFAULT FALSE,
    ow_regelingen   INT NOT NULL DEFAULT 0,
    wro_instrumenten INT NOT NULL DEFAULT 0,
    laatst_geladen  TIMESTAMP NULL,
    geldig_tot      DATE NULL
);

-- Gemeentegrenzen uit PDOK Bestuurlijke Gebieden. Levert de noemer
-- voor "% geponst" en de provincie-toewijzing per gemeente. Geladen
-- via src/loaders/gemeentegrens_pdok.py; eenmalig per jaar refreshen
-- i.v.m. gemeente-herindelingen.
CREATE TABLE IF NOT EXISTS core.gemeentegrens (
    overheidscode   TEXT PRIMARY KEY REFERENCES core.bronhouder(overheidscode),
    naam            TEXT NOT NULL,
    provincie       TEXT NULL,
    geometrie       GEOMETRY(MultiPolygon, 28992) NOT NULL,
    oppervlak_m2    DOUBLE PRECISION NOT NULL,
    peildatum       DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gemeentegrens_geom ON core.gemeentegrens USING GIST(geometrie);
CREATE INDEX IF NOT EXISTS idx_gemeentegrens_provincie ON core.gemeentegrens(provincie);

-- =============================================================
-- p2p.* — STOP: Regelingen en Besluiten
-- =============================================================

CREATE TABLE IF NOT EXISTS p2p.regeling (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regelingmodel       TEXT NOT NULL REFERENCES core.regelingmodel(code),
    opschrift           TEXT NOT NULL,
    soort_regeling      TEXT NULL,
    citeertitel         TEXT NULL,
    opvolger_van        TEXT NULL,
    is_tijdelijkdeel_van TEXT NULL,
    conditie            TEXT NULL,
    bronhouder          TEXT NULL REFERENCES core.bronhouder(overheidscode),
    documenttype        TEXT NULL REFERENCES core.documenttype(code),
    regelingsgebied_id  TEXT NULL,
    inactief            BOOLEAN     NOT NULL DEFAULT FALSE,
    datum_inactief      TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_regeling_work ON p2p.regeling(frbr_work);
CREATE INDEX IF NOT EXISTS idx_regeling_bronhouder ON p2p.regeling(bronhouder);
CREATE INDEX IF NOT EXISTS idx_regeling_inactief
    ON p2p.regeling(inactief) WHERE inactief = TRUE;

CREATE TABLE IF NOT EXISTS p2p.besluit (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    besluitmodel        TEXT NULL REFERENCES core.besluitmodel(code),
    bronhouder          TEXT NULL REFERENCES core.bronhouder(overheidscode)
);

CREATE TABLE IF NOT EXISTS p2p.besluit_regeling (
    besluit_expression  TEXT NOT NULL REFERENCES p2p.besluit(frbr_expression) ON DELETE CASCADE,
    regeling_expression TEXT NOT NULL REFERENCES p2p.regeling(frbr_expression) ON DELETE CASCADE,
    PRIMARY KEY (besluit_expression, regeling_expression)
);

CREATE TABLE IF NOT EXISTS p2p.procedurestap (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    besluit_expression  TEXT NOT NULL REFERENCES p2p.besluit(frbr_expression) ON DELETE CASCADE,
    soort               TEXT NOT NULL,
    datum               DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procedurestap_besluit ON p2p.procedurestap(besluit_expression);

CREATE TABLE IF NOT EXISTS p2p.tekst_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regeling_expression TEXT NOT NULL REFERENCES p2p.regeling(frbr_expression) ON DELETE CASCADE,
    eid                 TEXT NOT NULL,
    wid                 TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    parent_id           BIGINT NULL REFERENCES p2p.tekst_element(id) ON DELETE CASCADE,
    nummer              TEXT NULL,
    opschrift           TEXT NULL,
    inhoud              TEXT NULL,
    inhoud_plain        TEXT NULL,
    volgorde            INT NOT NULL DEFAULT 0,
    UNIQUE (regeling_expression, eid)
);
CREATE INDEX IF NOT EXISTS idx_tekst_element_regeling ON p2p.tekst_element(regeling_expression);
CREATE INDEX IF NOT EXISTS idx_tekst_element_parent ON p2p.tekst_element(parent_id);
CREATE INDEX IF NOT EXISTS idx_tekst_element_wid ON p2p.tekst_element(wid);
CREATE INDEX IF NOT EXISTS idx_tekst_element_inhoud_fts ON p2p.tekst_element
  USING gin (to_tsvector('dutch', coalesce(inhoud_plain, '')));

CREATE TABLE IF NOT EXISTS p2p.geo_informatieobject (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regeling_expression TEXT NULL REFERENCES p2p.regeling(frbr_expression),
    naam                TEXT NULL    -- geo:naam uit de GIO-GML; leesbare titel voor objectlijsten
);

CREATE TABLE IF NOT EXISTS p2p.juridische_borging (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gio_expression      TEXT NOT NULL REFERENCES p2p.geo_informatieobject(frbr_expression) ON DELETE CASCADE,
    domein              TEXT NOT NULL,
    domein_object_id    TEXT NOT NULL,
    locatie_id          TEXT NOT NULL,
    UNIQUE (gio_expression, domein_object_id, locatie_id)
);

-- =============================================================
-- p2p.* — CIM-OW: OW-Objecten
-- =============================================================

CREATE TABLE IF NOT EXISTS p2p.locatie (
    identificatie       TEXT PRIMARY KEY,
    locatie_type        TEXT NOT NULL,
    noemer              TEXT NULL,
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    bron                TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_locatie_geom ON p2p.locatie USING GIST(geometrie);

-- Afgeleide ("gematerialiseerde") tabel: p2p.locatie.geometrie opgedeeld via
-- ST_Subdivide(geometrie, 256), één rij per stukje (identificatie NIET uniek).
-- Regelingsgebieden zijn grote multipolygons; st_intersects op de volledige
-- geometrie kost seconden en duwt /v1/adres over de statement_timeout. Op de
-- opgedeelde stukjes pre-filtert de GiST-index veel preciezer (~5-90x sneller,
-- identieke resultaatset). Wordt gebruikt door _wat_geldt_hier (alle queries)
-- en de meeste geo-endpoints in ocd-api.
--
-- LET OP: deze tabel wordt NIET door de normale loader-INSERT gevuld. Na elke
-- (her)load van p2p.locatie moet hij herbouwd worden via ST_Subdivide — zie
-- dso-loader/scripts/herstel_*.py (refresh_subdiv*). Een verse DB krijgt hier
-- de lege tabel + indexen; de geo-queries geven dan lege resultaten tot een
-- subdiv-refresh is gedraaid.
CREATE TABLE IF NOT EXISTS p2p.locatie_subdiv (
    identificatie       TEXT NOT NULL,
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_locatie_subdiv_geom ON p2p.locatie_subdiv USING GIST(geometrie);
CREATE INDEX IF NOT EXISTS idx_locatie_subdiv_id   ON p2p.locatie_subdiv (identificatie);

CREATE TABLE IF NOT EXISTS p2p.locatiegroep_lid (
    groep_identificatie TEXT NOT NULL REFERENCES p2p.locatie(identificatie) ON DELETE CASCADE,
    lid_identificatie   TEXT NOT NULL REFERENCES p2p.locatie(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (groep_identificatie, lid_identificatie)
);

CREATE TABLE IF NOT EXISTS p2p.juridische_regel (
    identificatie       TEXT PRIMARY KEY,
    regel_type          TEXT NOT NULL,
    idealisatie         TEXT NULL REFERENCES core.idealisatie(code),
    thema               TEXT[] NULL,
    omschrijving        TEXT NULL,
    instructieregel_instrument      TEXT NULL,
    instructieregel_taakuitoefening TEXT NULL,
    regeltekst_wid      TEXT NOT NULL,
    -- Tot welke regeling hoort deze regel. Nodig om jr->tekst_element
    -- eenduidig te koppelen: regeltekst_wid (STOP wId) is NIET globaal uniek
    -- en wordt over regelingen hergebruikt (bv. template-voorbereidingsbesluiten
    -- pv27_1__chp_..., generieke Rijk-wIds body/recital). Zonder dit veld
    -- fan-out de join te.wid = jr.regeltekst_wid naar vreemde regelingen.
    regeling_expression TEXT NULL REFERENCES p2p.regeling(frbr_expression) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jr_regeltekst ON p2p.juridische_regel(regeltekst_wid);
-- Migratie voor bestaande databases (CREATE TABLE IF NOT EXISTS voegt geen kolom toe):
ALTER TABLE p2p.juridische_regel ADD COLUMN IF NOT EXISTS regeling_expression TEXT;
-- Composiet-index voor de gescopete join jr.regeltekst_wid + jr.regeling_expression:
CREATE INDEX IF NOT EXISTS idx_jr_regeltekst_expr
    ON p2p.juridische_regel(regeltekst_wid, regeling_expression);
-- Spiegelt p2p.tekst_element: index op (regeling_expression, wid) voor de join-kant:
CREATE INDEX IF NOT EXISTS idx_tekst_element_expr_wid
    ON p2p.tekst_element(regeling_expression, wid);

CREATE TABLE IF NOT EXISTS p2p.activiteit (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    bovenliggende       TEXT NULL REFERENCES p2p.activiteit(identificatie),
    is_tophaak          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_activiteit_bovenliggende ON p2p.activiteit(bovenliggende);

CREATE TABLE IF NOT EXISTS p2p.activiteit_locatieaanduiding (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    juridische_regel_id TEXT NOT NULL REFERENCES p2p.juridische_regel(identificatie) ON DELETE CASCADE,
    activiteit_id       TEXT NULL REFERENCES p2p.activiteit(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES p2p.locatie(identificatie),
    kwalificatie        TEXT NULL
);
-- activiteit_id is NULL voor Instructieregel/Omgevingswaardegel — die hebben
-- een directe <regels:locatieaanduiding> op regel-niveau (geen activiteit-koppeling).
-- Zonder deze inserts zouden hun werkingsgebieden onvindbaar zijn via coord-queries.
ALTER TABLE p2p.activiteit_locatieaanduiding ALTER COLUMN activiteit_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ala_regel ON p2p.activiteit_locatieaanduiding(juridische_regel_id);
CREATE INDEX IF NOT EXISTS idx_ala_activiteit ON p2p.activiteit_locatieaanduiding(activiteit_id);

CREATE TABLE IF NOT EXISTS p2p.gebiedsaanwijzing (
    identificatie       TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    locatie_id          TEXT NOT NULL REFERENCES p2p.locatie(identificatie)
);

CREATE TABLE IF NOT EXISTS p2p.juridische_regel_gebiedsaanwijzing (
    juridische_regel_id  TEXT NOT NULL REFERENCES p2p.juridische_regel(identificatie) ON DELETE CASCADE,
    gebiedsaanwijzing_id TEXT NOT NULL REFERENCES p2p.gebiedsaanwijzing(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, gebiedsaanwijzing_id)
);

CREATE TABLE IF NOT EXISTS p2p.norm (
    identificatie       TEXT PRIMARY KEY,
    norm_type           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    type_norm           TEXT NULL,
    eenheid             TEXT NULL,
    groep               TEXT NULL
);

CREATE TABLE IF NOT EXISTS p2p.normwaarde (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    norm_id             TEXT NOT NULL REFERENCES p2p.norm(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES p2p.locatie(identificatie),
    kwalitatieve_waarde TEXT NULL,
    kwantitatieve_waarde NUMERIC NULL,
    waarde_in_regeltekst BOOLEAN NULL
);

CREATE TABLE IF NOT EXISTS p2p.juridische_regel_norm (
    juridische_regel_id TEXT NOT NULL REFERENCES p2p.juridische_regel(identificatie) ON DELETE CASCADE,
    norm_id             TEXT NOT NULL REFERENCES p2p.norm(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, norm_id)
);

CREATE TABLE IF NOT EXISTS p2p.tekstdeel (
    identificatie       TEXT PRIMARY KEY,
    divisie_wid         TEXT NOT NULL,
    thema               TEXT[] NULL,
    locatie_id          TEXT NULL REFERENCES p2p.locatie(identificatie)
);

CREATE TABLE IF NOT EXISTS p2p.hoofdlijn (
    identificatie       TEXT PRIMARY KEY,
    soort               TEXT NOT NULL,
    naam                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p2p.tekstdeel_hoofdlijn (
    tekstdeel_id        TEXT NOT NULL REFERENCES p2p.tekstdeel(identificatie) ON DELETE CASCADE,
    hoofdlijn_id        TEXT NOT NULL REFERENCES p2p.hoofdlijn(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (tekstdeel_id, hoofdlijn_id)
);

CREATE TABLE IF NOT EXISTS p2p.pons (
    identificatie       TEXT PRIMARY KEY,
    locatie_id          TEXT NOT NULL REFERENCES p2p.locatie(identificatie),
    was_bestemmingsplan TEXT NULL
);

CREATE TABLE IF NOT EXISTS p2p.kaart (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p2p.kaartlaag (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kaart_id            TEXT NOT NULL REFERENCES p2p.kaart(identificatie) ON DELETE CASCADE,
    naam                TEXT NOT NULL,
    gebiedsaanwijzing_id TEXT NULL REFERENCES p2p.gebiedsaanwijzing(identificatie),
    norm_id             TEXT NULL REFERENCES p2p.norm(identificatie),
    activiteit_id       TEXT NULL REFERENCES p2p.activiteit(identificatie)
);

-- =============================================================
-- wro.* — Oud regime (Wro/IMRO), sunset 2032
-- =============================================================

CREATE TABLE IF NOT EXISTS wro.wro_manifest (
    overheidscode       TEXT PRIMARY KEY REFERENCES core.bronhouder(overheidscode),
    naam_overheid       TEXT NOT NULL,
    datum               DATE NULL
);

CREATE TABLE IF NOT EXISTS wro.wro_dossier (
    dossiernummer       TEXT PRIMARY KEY,
    manifest_code       TEXT NOT NULL REFERENCES wro.wro_manifest(overheidscode) ON DELETE CASCADE,
    status              TEXT NULL REFERENCES core.dossierstatus(code)
);

CREATE TABLE IF NOT EXISTS wro.ruimtelijk_instrument (
    idn                 TEXT PRIMARY KEY,
    dossier             TEXT NULL REFERENCES wro.wro_dossier(dossiernummer),
    type_plan           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    planstatus          TEXT NULL REFERENCES core.planstatus(code),
    datum               DATE NULL,
    bronhouder          TEXT NOT NULL REFERENCES core.bronhouder(overheidscode),
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    pons_status         TEXT NOT NULL DEFAULT 'actief',
    laatst_geladen      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wro_instrument_geom ON wro.ruimtelijk_instrument USING GIST(geometrie);
CREATE INDEX IF NOT EXISTS idx_wro_instrument_bronhouder ON wro.ruimtelijk_instrument(bronhouder);

CREATE TABLE IF NOT EXISTS wro.planobject (
    identificatie       TEXT PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES wro.ruimtelijk_instrument(idn) ON DELETE CASCADE,
    object_type         TEXT NOT NULL,
    naam                TEXT NULL,
    bestemmingshoofdgroep TEXT NULL REFERENCES core.bestemmingshoofdgroep(code),
    artikelnummer       TEXT NULL,
    bouwaanduidingtype  TEXT NULL REFERENCES core.bouwaanduidingtype(code),
    maatvoering_info    JSONB NULL,
    figuurtype          TEXT NULL REFERENCES core.figuurtype(code),
    gebiedsaanduidinghoofdgroep TEXT NULL REFERENCES core.gebiedsaanduidinghoofdgroep(code),
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    specificeert_id     TEXT NULL REFERENCES wro.planobject(identificatie)
);
CREATE INDEX IF NOT EXISTS idx_planobject_instrument ON wro.planobject(instrument_idn);
CREATE INDEX IF NOT EXISTS idx_planobject_geom ON wro.planobject USING GIST(geometrie);

CREATE TABLE IF NOT EXISTS wro.wro_tekst_object (
    identificatie       TEXT PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES wro.ruimtelijk_instrument(idn) ON DELETE CASCADE,
    volgnummer          INT NOT NULL,
    niveau              INT NOT NULL CHECK (niveau BETWEEN 0 AND 11),
    parent_id           TEXT NULL REFERENCES wro.wro_tekst_object(identificatie),
    object_type         TEXT NOT NULL,
    label               TEXT NULL,
    nummer              TEXT NULL,
    naam                TEXT NULL,
    inhoud              TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_wro_tekst_instrument ON wro.wro_tekst_object(instrument_idn);

CREATE TABLE IF NOT EXISTS wro.wro_geleideformulier (
    instrument_idn      TEXT PRIMARY KEY REFERENCES wro.ruimtelijk_instrument(idn) ON DELETE CASCADE,
    versie_imro         TEXT NOT NULL,
    versie_praktijkrichtlijn TEXT NOT NULL,
    datum               DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS wro.wro_bronbestand (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES wro.ruimtelijk_instrument(idn) ON DELETE CASCADE,
    bestandsnaam        TEXT NOT NULL,
    bestandstype        TEXT NOT NULL,
    lettercode          TEXT NULL,
    UNIQUE (instrument_idn, bestandsnaam)
);

-- =============================================================
-- i2a.* — IMTR: Toepasbare regels en werkzaamheden
-- =============================================================

CREATE TABLE IF NOT EXISTS i2a.regelbeheerobject (
    functionele_structuur_ref TEXT PRIMARY KEY,
    activiteit_id       TEXT NULL REFERENCES p2p.activiteit(identificatie),
    naam                TEXT NULL
);

CREATE TABLE IF NOT EXISTS i2a.toepasbaar_regelbestand (
    namespace           TEXT PRIMARY KEY,
    naam                TEXT NULL,
    sttr_versie         INT NOT NULL DEFAULT 1,
    geldig_begindatum   DATE NULL,
    geldig_einddatum    DATE NULL,
    regelbeheerobject   TEXT NULL REFERENCES i2a.regelbeheerobject(functionele_structuur_ref)
);

CREATE TABLE IF NOT EXISTS i2a.dmn_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regelbestand_ns     TEXT NOT NULL REFERENCES i2a.toepasbaar_regelbestand(namespace) ON DELETE CASCADE,
    dmn_id              TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    naam                TEXT NULL,
    parent_id           BIGINT NULL REFERENCES i2a.dmn_element(id),
    UNIQUE (regelbestand_ns, dmn_id)
);

CREATE TABLE IF NOT EXISTS i2a.uitvoeringsregel (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regelbestand_ns     TEXT NOT NULL REFERENCES i2a.toepasbaar_regelbestand(namespace) ON DELETE CASCADE,
    regel_type          TEXT NOT NULL,
    dmn_element_id      BIGINT NULL REFERENCES i2a.dmn_element(id),
    nen3610_id          TEXT NULL,
    activiteit_urn      TEXT NULL
);

CREATE TABLE IF NOT EXISTS i2a.werkzaamheid (
    urn                 TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    activiteit_id       TEXT NULL REFERENCES p2p.activiteit(identificatie)
);

CREATE TABLE IF NOT EXISTS i2a.aansluitpunt (
    uri                 TEXT PRIMARY KEY,
    type                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS i2a.aansluiting (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    aansluitpunt_uri    TEXT NOT NULL REFERENCES i2a.aansluitpunt(uri) ON DELETE CASCADE,
    activiteit_id       TEXT NULL REFERENCES p2p.activiteit(identificatie),
    bronhouder          TEXT NULL REFERENCES core.bronhouder(overheidscode),
    regelbestand_ns     TEXT NULL REFERENCES i2a.toepasbaar_regelbestand(namespace)
);

-- =============================================================
-- p2pwijziging.* — Ontwerpen en besluitversies (delta-gebaseerd)
-- =============================================================
-- Slaat alleen wijzigingen op (toevoegen/wijzigen/verwijderen)
-- t.o.v. de geconsolideerde versie in p2p. Filter:
-- alleen ontwerpen/besluiten die de huidige geldende versie
-- wijzigen OF in de toekomst in werking treden.

CREATE SCHEMA IF NOT EXISTS p2pwijziging;
COMMENT ON SCHEMA p2pwijziging IS 'Wijzigingen op geconsolideerde regelingen: ontwerpen en besluitversies, delta-gebaseerd';

CREATE TABLE IF NOT EXISTS p2pwijziging.besluit (
    ontwerpbesluit_id        TEXT PRIMARY KEY,
    technisch_id             TEXT NOT NULL UNIQUE,
    regeling_work            TEXT NOT NULL,
    wijzigt_expression       TEXT,
    nieuwe_expression        TEXT,
    soort                    TEXT NOT NULL,
    status                   TEXT NOT NULL,
    bekend_op                DATE,
    ontvangen_op             DATE,
    begin_geldigheid         DATE,
    begin_inwerking          DATE,
    eindverantwoordelijke    TEXT,
    bronhouder               TEXT REFERENCES core.bronhouder(overheidscode),
    documenttype             TEXT,
    opschrift                TEXT,
    citeertitel              TEXT,
    publicatie_id            TEXT,
    is_vervang_regeling      BOOLEAN NOT NULL DEFAULT FALSE,
    beschikbaar_op           TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ontwerp_besluit_work ON p2pwijziging.besluit(regeling_work);
CREATE INDEX IF NOT EXISTS idx_ontwerp_besluit_wijzigt ON p2pwijziging.besluit(wijzigt_expression);
CREATE INDEX IF NOT EXISTS idx_ontwerp_besluit_inwerking ON p2pwijziging.besluit(begin_inwerking);
CREATE INDEX IF NOT EXISTS idx_ontwerp_besluit_status ON p2pwijziging.besluit(status);

CREATE TABLE IF NOT EXISTS p2pwijziging.procedurestap (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id        TEXT NOT NULL REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    soort                    TEXT NOT NULL,
    voltooid_op              DATE,
    plaats                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ontwerp_procedurestap ON p2pwijziging.procedurestap(ontwerpbesluit_id);

-- Documentstructuur als volle boom (mirror van p2p.tekst_element) met
-- renvooi-attributen op de gewijzigde nodes. Geen sparse delta-tabel —
-- de DSO-API levert sowieso de volle boom, en mirror geeft viewer-
-- symmetrie + FTS gratis. Zie 2026-05-refactor-p2pwijziging-tekst.sql
-- voor de motivatie.
CREATE TABLE IF NOT EXISTS p2pwijziging.tekst_element (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id        TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    eid                      TEXT NOT NULL,
    wid                      TEXT NOT NULL,
    element_type             TEXT NOT NULL,
    parent_id                BIGINT NULL
        REFERENCES p2pwijziging.tekst_element(id) ON DELETE CASCADE,
    nummer                   TEXT NULL,
    opschrift                TEXT NULL,
    inhoud                   TEXT NULL,
    inhoud_plain             TEXT GENERATED ALWAYS AS (
        regexp_replace(
            regexp_replace(COALESCE(inhoud, ''), '<[^>]+>', ' ', 'g'),
            '\\s+', ' ', 'g'
        )
    ) STORED,
    volgorde                 INT NOT NULL DEFAULT 0,
    wijzigactie              TEXT NULL CHECK (wijzigactie IN
        ('voegtoe', 'verwijder', 'nieuweContainer', 'verwijderContainer')),
    vervallen                BOOLEAN NOT NULL DEFAULT FALSE,
    bevat_renvooi            BOOLEAN NOT NULL DEFAULT FALSE,
    bevat_ontwerp_informatie BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (ontwerpbesluit_id, eid)
);
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_besluit
    ON p2pwijziging.tekst_element(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_parent
    ON p2pwijziging.tekst_element(parent_id);
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_wid
    ON p2pwijziging.tekst_element(wid);
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_wijzigactie
    ON p2pwijziging.tekst_element(ontwerpbesluit_id, wijzigactie)
    WHERE wijzigactie IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_renvooi
    ON p2pwijziging.tekst_element(ontwerpbesluit_id)
    WHERE bevat_renvooi = TRUE;
CREATE INDEX IF NOT EXISTS idx_pw_tekst_element_inhoud_fts
    ON p2pwijziging.tekst_element
    USING gin (to_tsvector('dutch', coalesce(inhoud_plain, '')));

CREATE TABLE IF NOT EXISTS p2pwijziging.annotatie_delta (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id        TEXT NOT NULL REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    type                     TEXT NOT NULL,
    identificatie            TEXT NOT NULL,
    bewerking                TEXT NOT NULL CHECK (bewerking IN ('toevoegen', 'wijzigen', 'verwijderen')),
    naam                     TEXT,
    payload                  JSONB NOT NULL,
    UNIQUE (ontwerpbesluit_id, type, identificatie)
);
CREATE INDEX IF NOT EXISTS idx_ontwerp_ann_besluit ON p2pwijziging.annotatie_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_ontwerp_ann_type ON p2pwijziging.annotatie_delta(type);
CREATE INDEX IF NOT EXISTS idx_ontwerp_ann_id ON p2pwijziging.annotatie_delta(identificatie);

CREATE TABLE IF NOT EXISTS p2pwijziging.locatie_delta (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id        TEXT NOT NULL REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    locatie_id               TEXT NOT NULL,
    bewerking                TEXT NOT NULL CHECK (bewerking IN ('toevoegen', 'wijzigen', 'verwijderen')),
    locatie_type             TEXT,
    noemer                   TEXT,
    geometrie                GEOMETRY(Geometry, 28992),
    UNIQUE (ontwerpbesluit_id, locatie_id)
);
CREATE INDEX IF NOT EXISTS idx_ontwerp_loc_besluit ON p2pwijziging.locatie_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_ontwerp_loc_geom ON p2pwijziging.locatie_delta USING GIST(geometrie);

-- Check constraints op besluit
ALTER TABLE p2pwijziging.besluit DROP CONSTRAINT IF EXISTS besluit_soort_check;
ALTER TABLE p2pwijziging.besluit DROP CONSTRAINT IF EXISTS besluit_status_check;
ALTER TABLE p2pwijziging.besluit
  ADD CONSTRAINT besluit_soort_check CHECK (soort IN ('ontwerp', 'besluitversie')),
  ADD CONSTRAINT besluit_status_check CHECK (status IN ('ontwerp', 'ter_inzage', 'vastgesteld', 'in_werking'));

-- Views voor expliciete leesbaarheid per soort
CREATE OR REPLACE VIEW p2pwijziging.ontwerp AS
  SELECT * FROM p2pwijziging.besluit WHERE soort = 'ontwerp';
CREATE OR REPLACE VIEW p2pwijziging.besluitversie AS
  SELECT * FROM p2pwijziging.besluit WHERE soort = 'besluitversie';

-- =============================================================
-- v2a.* — Vraag-tot-antwoord: viewer-aggregaties
-- =============================================================
-- Later uitbreidbaar met: vergunning, vergunning_locatie,
-- zoekindex-caches. Nu: aggregaties voor publieke trackers.

-- Ponsenkaart-aggregatie per gemeente. Eén rij per gemeente met
-- huidige stand: cumulatief geponst oppervlak (ST_Union zodat
-- overlappende ponsen niet dubbel tellen), aantal ponsen, en
-- afgeleid percentage.
--
-- Koppeling pons → gemeente: RUIMTELIJK via ST_Within(centroid, gemeente).
-- We gebruiken niet p2p.pons.was_bestemmingsplan omdat de huidige
-- OW-loader die kolom niet vult (parse_ponsen leest enkel id+locatie_ref
-- uit ponsen.xml). Spatial join is bovendien onafhankelijk van of de
-- gemeente Wro-data heeft.
--
-- Refresh nachtelijk of na OW-ingest met REFRESH MATERIALIZED VIEW
-- CONCURRENTLY.
CREATE MATERIALIZED VIEW IF NOT EXISTS v2a.ponsenkaart_gemeente_stats AS
WITH pons_geom AS (
    SELECT p.identificatie AS pons_id,
           l.geometrie     AS geometrie,
           ST_Centroid(l.geometrie) AS centroid
      FROM p2p.pons p
      JOIN p2p.locatie l ON l.identificatie = p.locatie_id
)
SELECT
    g.overheidscode,
    g.naam,
    g.provincie,
    g.oppervlak_m2                                       AS gemeente_opp_m2,
    COALESCE(ST_Area(ST_Union(pg.geometrie)), 0)         AS geponst_opp_m2,
    COUNT(pg.pons_id)                                    AS pons_count,
    CASE WHEN g.oppervlak_m2 > 0
         THEN ROUND((COALESCE(ST_Area(ST_Union(pg.geometrie)), 0)
                     / g.oppervlak_m2 * 100)::numeric, 2)
         ELSE 0
    END                                                  AS pct
FROM core.gemeentegrens g
LEFT JOIN pons_geom pg
       ON ST_Within(pg.centroid, g.geometrie)
GROUP BY g.overheidscode, g.naam, g.provincie, g.oppervlak_m2;

-- Concurrent-refresh vereist een unique index
CREATE UNIQUE INDEX IF NOT EXISTS idx_ponsenkaart_stats_pk
    ON v2a.ponsenkaart_gemeente_stats(overheidscode);
CREATE INDEX IF NOT EXISTS idx_ponsenkaart_stats_provincie
    ON v2a.ponsenkaart_gemeente_stats(provincie);

-- =============================================================
-- conv.* — Conversie-output: bestemmingsplan → omgevingsplan
-- =============================================================
-- Afgeleid, herhaalbaar. Zelfde structuur als p2p, apart schema
-- zodat autoritatieve data en conversie-voorstellen niet mengen.

CREATE SCHEMA IF NOT EXISTS conv;
COMMENT ON SCHEMA conv IS 'Conversie-output: bestemmingsplan -> omgevingsplan (afgeleid, herhaalbaar)';

CREATE TABLE IF NOT EXISTS conv.conversie_meta (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_idn      TEXT NOT NULL,
    regeling_expression TEXT NOT NULL,
    stap                INT NOT NULL,
    bron                TEXT NOT NULL,
    geconverteerd_op    TIMESTAMP NOT NULL DEFAULT NOW(),
    llm_model           TEXT NULL,
    notities            TEXT NULL
);

CREATE TABLE IF NOT EXISTS conv.regeling (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regelingmodel       TEXT NOT NULL,
    opschrift           TEXT NOT NULL,
    bronhouder          TEXT NULL REFERENCES core.bronhouder(overheidscode),
    documenttype        TEXT NULL
);

CREATE TABLE IF NOT EXISTS conv.tekst_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regeling_expression TEXT NOT NULL REFERENCES conv.regeling(frbr_expression) ON DELETE CASCADE,
    eid                 TEXT NOT NULL,
    wid                 TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    parent_id           BIGINT NULL REFERENCES conv.tekst_element(id) ON DELETE CASCADE,
    nummer              TEXT NULL,
    opschrift           TEXT NULL,
    inhoud              TEXT NULL,
    volgorde            INT NOT NULL DEFAULT 0,
    UNIQUE (regeling_expression, eid)
);

CREATE TABLE IF NOT EXISTS conv.locatie (
    identificatie       TEXT PRIMARY KEY,
    locatie_type        TEXT NOT NULL,
    noemer              TEXT NULL,
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    bron_planobject     TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_locatie_geom ON conv.locatie USING GIST(geometrie);

CREATE TABLE IF NOT EXISTS conv.locatiegroep_lid (
    groep_identificatie TEXT NOT NULL REFERENCES conv.locatie(identificatie) ON DELETE CASCADE,
    lid_identificatie   TEXT NOT NULL REFERENCES conv.locatie(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (groep_identificatie, lid_identificatie)
);

CREATE TABLE IF NOT EXISTS conv.gebiedsaanwijzing (
    identificatie       TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    bron                TEXT NOT NULL DEFAULT 'mechanisch'
);

CREATE TABLE IF NOT EXISTS conv.activiteit (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    bovenliggende       TEXT NULL REFERENCES conv.activiteit(identificatie),
    is_tophaak          BOOLEAN NOT NULL DEFAULT FALSE,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel (
    identificatie       TEXT PRIMARY KEY,
    regel_type          TEXT NOT NULL,
    thema               TEXT[] NULL,
    regeltekst_wid      TEXT NOT NULL,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.activiteit_locatieaanduiding (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    juridische_regel_id TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    activiteit_id       TEXT NOT NULL REFERENCES conv.activiteit(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    kwalificatie        TEXT NULL
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel_gebiedsaanwijzing (
    juridische_regel_id  TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    gebiedsaanwijzing_id TEXT NOT NULL REFERENCES conv.gebiedsaanwijzing(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, gebiedsaanwijzing_id)
);

CREATE TABLE IF NOT EXISTS conv.norm (
    identificatie       TEXT PRIMARY KEY,
    norm_type           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    type_norm           TEXT NULL,
    eenheid             TEXT NULL,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.normwaarde (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    norm_id             TEXT NOT NULL REFERENCES conv.norm(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    kwalitatieve_waarde TEXT NULL,
    kwantitatieve_waarde NUMERIC NULL
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel_norm (
    juridische_regel_id TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    norm_id             TEXT NOT NULL REFERENCES conv.norm(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, norm_id)
);
"""

LOOKUPS = """
SET search_path TO p2p, wro, i2a, v2a, core, public;

INSERT INTO core.bestemmingshoofdgroep (code) VALUES
('Agrarisch'),('Agrarisch met waarden'),('Bedrijf'),('Bedrijventerrein'),
('Bos'),('Centrum'),('Cultuur en ontspanning'),('Detailhandel'),
('Dienstverlening'),('Gemengd'),('Groen'),('Horeca'),('Kantoor'),
('Maatschappelijk'),('Natuur'),('Recreatie'),('Sport'),('Tuin'),
('Verkeer'),('Water'),('Wonen'),('Woongebied'),('Overig')
ON CONFLICT DO NOTHING;

INSERT INTO core.dubbelbestemmingshoofdgroep (code) VALUES
('Leiding'),('Waarde'),('Waterstaat')
ON CONFLICT DO NOTHING;

INSERT INTO core.bouwaanduidingtype (code) VALUES
('aaneengebouwd'),('antennemast'),('bijgebouwen'),('gestapeld'),
('kap'),('karakteristiek'),('nokrichting'),('onderdoorgang'),
('plat dak'),('twee-aaneen'),('vrijstaand'),('specifieke bouwaanduiding')
ON CONFLICT DO NOTHING;

INSERT INTO core.figuurtype (code) VALUES
('as van de weg'),('dwarsprofiel'),('gevellijn'),('hartlijn leiding'),
('hartlijn leiding - brandstof'),('hartlijn leiding - gas'),
('hartlijn leiding - hoogspanning'),('hartlijn leiding - hoogspanningsverbinding'),
('hartlijn leiding - olie'),('hartlijn leiding - riool'),
('hartlijn leiding - water'),('relatie')
ON CONFLICT DO NOTHING;

INSERT INTO core.gebiedsaanduidinghoofdgroep (code) VALUES
('geluidzone'),('luchtvaartverkeerzone'),('milieuzone'),
('reconstructiewetzone'),('veiligheidszone'),('vrijwaringszone'),
('wetgevingzone'),('overige zone')
ON CONFLICT DO NOTHING;

INSERT INTO core.dossierstatus (code) VALUES
('in voorbereiding'),('vastgesteld'),('geheel in werking'),
('deels in werking'),('niet in werking'),
('geheel onherroepelijk in werking'),('deels onherroepelijk in werking'),
('vervallen'),('geconsolideerd')
ON CONFLICT DO NOTHING;

INSERT INTO core.planstatus (code) VALUES
('concept'),('voorontwerp'),('ontwerp'),('vastgesteld'),('geconsolideerd')
ON CONFLICT DO NOTHING;

INSERT INTO core.regelingmodel (code) VALUES
('RegelingKlassiek'),('RegelingCompact'),
('RegelingTijdelijkdeel'),('RegelingVrijetekst')
ON CONFLICT DO NOTHING;

INSERT INTO core.besluitmodel (code) VALUES
('BesluitKlassiek'),('BesluitCompact')
ON CONFLICT DO NOTHING;

INSERT INTO core.publicatiebladtype (code) VALUES
('Staatsblad'),('Staatscourant'),('Gemeenteblad'),
('Provinciaalblad'),('Waterschapsblad'),('BladGR')
ON CONFLICT DO NOTHING;

INSERT INTO core.idealisatie (code) VALUES
('exact'),('indicatief')
ON CONFLICT DO NOTHING;

INSERT INTO core.toestemmingstype (code) VALUES
('Vergunningplicht'),('Meldingsplicht'),('Informatieplicht'),
('Verbod'),('Gebod'),('Toegestaan'),('Anders geduid')
ON CONFLICT DO NOTHING;

INSERT INTO core.documenttype (code) VALUES
('Omgevingsplan'),('Omgevingsverordening'),('Waterschapsverordening'),
('Omgevingsvisie'),('Programma'),('Projectbesluit'),
('AMvB'),('Ministeriele regeling'),('Instructie'),
('Voorbereidingsbesluit'),('Reactieve interventie'),
('Natura 2000-besluit'),
('Voorbeschermingsregels'),
('Voorbeschermingsregels Omgevingsplan'),
('Voorbeschermingsregels Omgevingsverordening')
ON CONFLICT DO NOTHING;
"""


# =============================================================================
# KOOP-DDL — losstaand schema voor omgevingsvergunning-kennisgevingen
# =============================================================================
#
# Bewust geen FK's naar dso.bevoegd_gezag of andere tabellen. De loader haalt
# records uit KOOP SRU en persisteert ze hier zonder verdere koppeling.
# Activiteit (KOOP-waardelijst) en bg_naam (vrije vorm) blijven tekstwaarden;
# eventuele mapping naar IMOW-Activiteit en bestaande BevoegdGezag-tabel is
# een latere keuze.
#
# Achtergrond: zie vault_v1/analysis/Ingest omgevingsvergunningen uit
# officielebekendmakingen.md en vault_v1/model.md §14.
#
# De PoC-versie van deze DDL in scripts/koop-poc/schema.sql is een
# wrapper-kopie voor `python ingest.py setup`-compatibiliteit; deze KOOP_DDL
# is de canonical bron.

KOOP_DDL = """
CREATE SCHEMA IF NOT EXISTS vth;

CREATE TABLE IF NOT EXISTS vth.vergunningkennisgeving (
    koop_id              TEXT PRIMARY KEY,
    -- Publicatieblad-prefix uit KOOP (gmb, prb, wsb, stcrt, stb, trb, bgr, …).
    -- Bewust geen CHECK-constraint: KOOP voegt regelmatig nieuwe bladen toe
    -- (bv. bgr = Blad gemeenschappelijke regeling) en we willen dat niet
    -- elke keer eerst in de allow-list moeten zetten.
    publicatieblad       TEXT NOT NULL,

    -- Bevoegd gezag — vrije vorm zoals KOOP geeft (geen FK)
    bg_naam              TEXT NOT NULL,
    bg_scheme            TEXT,
    organisatietype      TEXT,

    -- Identificatie
    titel                TEXT NOT NULL,
    datum_publicatie     DATE NOT NULL,
    jaargang             INTEGER,
    publicatienummer     TEXT,
    rubriek              TEXT,

    -- Classificaties
    activiteit_code      TEXT,  -- OVERHEIDop.ActiviteitOmgevingsvergunning
    type_besluit         TEXT
        CHECK (type_besluit IS NULL OR type_besluit IN (
            'aanvraag', 'verleend', 'geweigerd', 'ontwerp',
            'van_rechtswege', 'ingetrokken', 'verlenging_beslistermijn',
            'melding', 'melding_geaccepteerd', 'kennisgeving',
            'rectificatie', 'overig'
        )),

    -- Geometrie (PostGIS; 95% gevuld in PoC)
    geometrie_type       TEXT
        CHECK (geometrie_type IS NULL OR geometrie_type IN (
            'Adres', 'Punt', 'Vlak', 'Postcodegebied'
        )),
    geometrie_rd         GEOMETRY(GEOMETRY, 28992),  -- echte geometrie
    geometrie_rd_pt      GEOMETRY(POINT, 28992),     -- centroid / punt
    geometrie_wgs_pt     GEOMETRY(POINT, 4326),
    geometrielabel       TEXT,                       -- adresstring uit KOOP (vooral bij Vlak)

    -- Adresvelden
    postcode             TEXT,
    huisnummer           TEXT,
    huisletter           TEXT,
    huisnummertoevoeging TEXT,
    straatnaam           TEXT,
    woonplaats           TEXT,
    ligt_in_gemeente     TEXT,

    -- Volledige inhoud
    beschrijving         TEXT,
    preferred_url        TEXT,
    xml_url              TEXT,
    pdf_url              TEXT,                           -- gzd:itemUrl manifestation="pdf"
    raw_xml              TEXT NOT NULL,                  -- SRU-record (metadata)
    inhoud_xml           TEXT,                           -- volledige publicatie-XML
    inhoud_tekst         TEXT,                           -- platte tekst (alle <al>/<li>)
    inhoud_geladen_at    TIMESTAMPTZ,                    -- NULL = enrichment nog niet gedaan
    zaaknummer_bg        TEXT,                           -- geparst uit inhoud_tekst
    datum_ontvangst      DATE,                           -- aanvraagdatum, geparst uit inhoud_tekst

    -- Extra SRU-metadata (sinds 2026-05-20 — zie gaps#G-70 update 2026-05-20)
    datum_publicatie_ts  TIMESTAMPTZ,                    -- cup:datumTijdstipWijzigingWork (fijnere tijd dan datum_publicatie)
    subject_taxonomie    TEXT,                           -- dcterms:subject OVERHEID.TaxonomieBeleidsagendaDecentraal

    -- Pipeline-metadata
    ingest_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingest_run_id        TEXT
);

-- Idempotente kolom-toevoegingen voor bestaande DB's (Postgres 9.6+).
-- Houdt setup-koop veilig om herhaaldelijk te draaien.
ALTER TABLE vth.vergunningkennisgeving
    ADD COLUMN IF NOT EXISTS pdf_url             TEXT,
    ADD COLUMN IF NOT EXISTS datum_ontvangst     DATE,
    ADD COLUMN IF NOT EXISTS datum_publicatie_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS subject_taxonomie   TEXT;

CREATE INDEX IF NOT EXISTS idx_vk_bg_datum
    ON vth.vergunningkennisgeving (bg_naam, datum_publicatie DESC);
CREATE INDEX IF NOT EXISTS idx_vk_datum
    ON vth.vergunningkennisgeving (datum_publicatie DESC);
CREATE INDEX IF NOT EXISTS idx_vk_activiteit
    ON vth.vergunningkennisgeving (activiteit_code);
CREATE INDEX IF NOT EXISTS idx_vk_type_besluit
    ON vth.vergunningkennisgeving (type_besluit);
CREATE INDEX IF NOT EXISTS idx_vk_publicatieblad
    ON vth.vergunningkennisgeving (publicatieblad);
CREATE INDEX IF NOT EXISTS idx_vk_geom_rd_pt
    ON vth.vergunningkennisgeving USING gist (geometrie_rd_pt);
CREATE INDEX IF NOT EXISTS idx_vk_geom_wgs_pt
    ON vth.vergunningkennisgeving USING gist (geometrie_wgs_pt);
CREATE INDEX IF NOT EXISTS idx_vk_tsv
    ON vth.vergunningkennisgeving USING gin (
        to_tsvector('dutch',
            coalesce(titel, '') || ' ' ||
            coalesce(beschrijving, '') || ' ' ||
            coalesce(inhoud_tekst, '') || ' ' ||
            coalesce(straatnaam, '') || ' ' ||
            coalesce(woonplaats, '')
        )
    );
CREATE INDEX IF NOT EXISTS idx_vk_zaaknummer
    ON vth.vergunningkennisgeving (zaaknummer_bg);
CREATE INDEX IF NOT EXISTS idx_vk_inhoud_geladen
    ON vth.vergunningkennisgeving (inhoud_geladen_at)
    WHERE inhoud_geladen_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vk_subject_taxonomie
    ON vth.vergunningkennisgeving (subject_taxonomie);
CREATE INDEX IF NOT EXISTS idx_vk_datum_ontvangst
    ON vth.vergunningkennisgeving (datum_ontvangst)
    WHERE datum_ontvangst IS NOT NULL;

-- Run-tracking (analoog aan etl_run in SQLite-PoC)
CREATE TABLE IF NOT EXISTS vth.etl_run (
    run_id            TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    processed_date    DATE NOT NULL,
    record_count      INTEGER,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL,
    error             TEXT,
    UNIQUE (source, processed_date)
);

-- Directe-deeplinks naar het inhoudelijke besluit-dossier.
--
-- Een 'deeplink' is een URL in <extref>/<intref>/<a> binnen de publicatie-XML
-- die naar een uniek dossier wijst (geen algemene landingspagina). De whitelist
-- van hosts staat in src.loaders.koop_deeplinks en wordt periodiek uitgebreid
-- op basis van nieuwe deeplink-host-analyse.
--
-- Validatie (http_status/gevalideerd_at) is OPTIONEEL: bij backfill voeren we
-- meteen een HTTP-check uit; bij de doorlopende enrich-pass blijven de velden
-- NULL tot een aparte validate-pass loopt.
CREATE TABLE IF NOT EXISTS vth.vergunning_deeplink (
    id              BIGSERIAL PRIMARY KEY,
    koop_id         TEXT NOT NULL
                       REFERENCES vth.vergunningkennisgeving(koop_id)
                       ON DELETE CASCADE,
    inzage_url      TEXT NOT NULL,
    host            TEXT NOT NULL,
    bron_element    TEXT NOT NULL,           -- 'extref@doc' | 'extref text' | 'intref@ref' | 'a@href'
    gevonden_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Validatie (NULL = nog niet gecheckt)
    http_status     INTEGER,
    final_url       TEXT,
    content_length  INTEGER,
    gevalideerd_at  TIMESTAMPTZ,
    werkt           BOOLEAN GENERATED ALWAYS AS
                       (http_status BETWEEN 200 AND 299) STORED,

    UNIQUE (koop_id, inzage_url)
);

CREATE INDEX IF NOT EXISTS idx_deeplink_koop
    ON vth.vergunning_deeplink (koop_id);
CREATE INDEX IF NOT EXISTS idx_deeplink_host
    ON vth.vergunning_deeplink (host);
CREATE INDEX IF NOT EXISTS idx_deeplink_werkt
    ON vth.vergunning_deeplink (koop_id) WHERE werkt;
CREATE INDEX IF NOT EXISTS idx_deeplink_unvalidated
    ON vth.vergunning_deeplink (gevonden_at) WHERE gevalideerd_at IS NULL;
"""
