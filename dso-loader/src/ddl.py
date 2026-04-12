"""DDL statements for the DSO database schema.

Based on analysis/Datamodel v1.0 DDL.md in the vault.
Scope: ABCE, snapshot-only, read-only, all bronhouders.
"""

DDL = """
-- =============================================================
-- DSO Datamodel v1.0 — Postgres + PostGIS
-- Scope: ABCE (Ow + IMTR + Wro + waardelijsten)
-- =============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS dso;
SET search_path TO dso, public;

-- =============================================================
-- 1. Lookup-tabellen
-- =============================================================

CREATE TABLE IF NOT EXISTS bestemmingshoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS dubbelbestemmingshoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS bouwaanduidingtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS maatvoeringsaanduiding (
    code TEXT PRIMARY KEY,
    eenheid TEXT NULL
);

CREATE TABLE IF NOT EXISTS figuurtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS gebiedsaanduidinghoofdgroep (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS dossierstatus (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS planstatus (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS regelingmodel (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS besluitmodel (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS publicatiebladtype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS idealisatie (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS toestemmingstype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS documenttype (
    code TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS waardelijst (
    uri             TEXT PRIMARY KEY,
    waardelijst     TEXT NOT NULL,
    label           TEXT NOT NULL,
    beschrijving    TEXT NULL,
    geldig_vanaf    DATE NULL,
    geldig_tot      DATE NULL
);

-- =============================================================
-- 2. Bronhouder
-- =============================================================

CREATE TABLE IF NOT EXISTS bronhouder (
    overheidscode   TEXT PRIMARY KEY,
    naam            TEXT NOT NULL
);

-- =============================================================
-- 3. STOP — Regelingen en Besluiten
-- =============================================================

CREATE TABLE IF NOT EXISTS regeling (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regelingmodel       TEXT NOT NULL REFERENCES regelingmodel(code),
    opschrift           TEXT NOT NULL,
    soort_regeling      TEXT NULL,
    citeertitel         TEXT NULL,
    opvolger_van        TEXT NULL,
    is_tijdelijkdeel_van TEXT NULL,
    conditie            TEXT NULL,
    bronhouder          TEXT NULL REFERENCES bronhouder(overheidscode),
    documenttype        TEXT NULL REFERENCES documenttype(code),
    regelingsgebied_id  TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_regeling_work ON regeling(frbr_work);
CREATE INDEX IF NOT EXISTS idx_regeling_bronhouder ON regeling(bronhouder);

CREATE TABLE IF NOT EXISTS besluit (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    besluitmodel        TEXT NULL REFERENCES besluitmodel(code),
    bronhouder          TEXT NULL REFERENCES bronhouder(overheidscode)
);

CREATE TABLE IF NOT EXISTS besluit_regeling (
    besluit_expression  TEXT NOT NULL REFERENCES besluit(frbr_expression) ON DELETE CASCADE,
    regeling_expression TEXT NOT NULL REFERENCES regeling(frbr_expression) ON DELETE CASCADE,
    PRIMARY KEY (besluit_expression, regeling_expression)
);

CREATE TABLE IF NOT EXISTS procedurestap (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    besluit_expression  TEXT NOT NULL REFERENCES besluit(frbr_expression) ON DELETE CASCADE,
    soort               TEXT NOT NULL,
    datum               DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procedurestap_besluit ON procedurestap(besluit_expression);

CREATE TABLE IF NOT EXISTS tekst_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regeling_expression TEXT NOT NULL REFERENCES regeling(frbr_expression) ON DELETE CASCADE,
    eid                 TEXT NOT NULL,
    wid                 TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    parent_id           BIGINT NULL REFERENCES tekst_element(id) ON DELETE CASCADE,
    nummer              TEXT NULL,
    opschrift           TEXT NULL,
    inhoud              TEXT NULL,
    volgorde            INT NOT NULL DEFAULT 0,
    UNIQUE (regeling_expression, eid)
);
CREATE INDEX IF NOT EXISTS idx_tekst_element_regeling ON tekst_element(regeling_expression);
CREATE INDEX IF NOT EXISTS idx_tekst_element_parent ON tekst_element(parent_id);
CREATE INDEX IF NOT EXISTS idx_tekst_element_wid ON tekst_element(wid);

CREATE TABLE IF NOT EXISTS geo_informatieobject (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regeling_expression TEXT NULL REFERENCES regeling(frbr_expression)
);

CREATE TABLE IF NOT EXISTS juridische_borging (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gio_expression      TEXT NOT NULL REFERENCES geo_informatieobject(frbr_expression) ON DELETE CASCADE,
    domein              TEXT NOT NULL,
    domein_object_id    TEXT NOT NULL,
    locatie_id          TEXT NOT NULL,
    UNIQUE (gio_expression, domein_object_id, locatie_id)
);

-- =============================================================
-- 4. CIM-OW — OW-Objecten
-- =============================================================

CREATE TABLE IF NOT EXISTS locatie (
    identificatie       TEXT PRIMARY KEY,
    locatie_type        TEXT NOT NULL,
    noemer              TEXT NULL,
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    bron                TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_locatie_geom ON locatie USING GIST(geometrie);

CREATE TABLE IF NOT EXISTS locatiegroep_lid (
    groep_identificatie TEXT NOT NULL REFERENCES locatie(identificatie) ON DELETE CASCADE,
    lid_identificatie   TEXT NOT NULL REFERENCES locatie(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (groep_identificatie, lid_identificatie)
);

CREATE TABLE IF NOT EXISTS juridische_regel (
    identificatie       TEXT PRIMARY KEY,
    regel_type          TEXT NOT NULL,
    idealisatie         TEXT NULL REFERENCES idealisatie(code),
    thema               TEXT[] NULL,
    omschrijving        TEXT NULL,
    instructieregel_instrument      TEXT NULL,
    instructieregel_taakuitoefening TEXT NULL,
    regeltekst_wid      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jr_regeltekst ON juridische_regel(regeltekst_wid);

CREATE TABLE IF NOT EXISTS activiteit (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    bovenliggende       TEXT NULL REFERENCES activiteit(identificatie),
    is_tophaak          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_activiteit_bovenliggende ON activiteit(bovenliggende);

CREATE TABLE IF NOT EXISTS activiteit_locatieaanduiding (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    juridische_regel_id TEXT NOT NULL REFERENCES juridische_regel(identificatie) ON DELETE CASCADE,
    activiteit_id       TEXT NOT NULL REFERENCES activiteit(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES locatie(identificatie),
    kwalificatie        TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_ala_regel ON activiteit_locatieaanduiding(juridische_regel_id);
CREATE INDEX IF NOT EXISTS idx_ala_activiteit ON activiteit_locatieaanduiding(activiteit_id);

CREATE TABLE IF NOT EXISTS gebiedsaanwijzing (
    identificatie       TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    locatie_id          TEXT NOT NULL REFERENCES locatie(identificatie)
);

CREATE TABLE IF NOT EXISTS juridische_regel_gebiedsaanwijzing (
    juridische_regel_id  TEXT NOT NULL REFERENCES juridische_regel(identificatie) ON DELETE CASCADE,
    gebiedsaanwijzing_id TEXT NOT NULL REFERENCES gebiedsaanwijzing(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, gebiedsaanwijzing_id)
);

CREATE TABLE IF NOT EXISTS norm (
    identificatie       TEXT PRIMARY KEY,
    norm_type           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    type_norm           TEXT NULL,
    eenheid             TEXT NULL,
    groep               TEXT NULL
);

CREATE TABLE IF NOT EXISTS normwaarde (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    norm_id             TEXT NOT NULL REFERENCES norm(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES locatie(identificatie),
    kwalitatieve_waarde TEXT NULL,
    kwantitatieve_waarde NUMERIC NULL
);

CREATE TABLE IF NOT EXISTS juridische_regel_norm (
    juridische_regel_id TEXT NOT NULL REFERENCES juridische_regel(identificatie) ON DELETE CASCADE,
    norm_id             TEXT NOT NULL REFERENCES norm(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, norm_id)
);

CREATE TABLE IF NOT EXISTS tekstdeel (
    identificatie       TEXT PRIMARY KEY,
    divisie_wid         TEXT NOT NULL,
    thema               TEXT[] NULL,
    locatie_id          TEXT NULL REFERENCES locatie(identificatie)
);

CREATE TABLE IF NOT EXISTS hoofdlijn (
    identificatie       TEXT PRIMARY KEY,
    soort               TEXT NOT NULL,
    naam                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tekstdeel_hoofdlijn (
    tekstdeel_id        TEXT NOT NULL REFERENCES tekstdeel(identificatie) ON DELETE CASCADE,
    hoofdlijn_id        TEXT NOT NULL REFERENCES hoofdlijn(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (tekstdeel_id, hoofdlijn_id)
);

CREATE TABLE IF NOT EXISTS pons (
    identificatie       TEXT PRIMARY KEY,
    locatie_id          TEXT NOT NULL REFERENCES locatie(identificatie),
    was_bestemmingsplan TEXT NULL
);

CREATE TABLE IF NOT EXISTS kaart (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kaartlaag (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kaart_id            TEXT NOT NULL REFERENCES kaart(identificatie) ON DELETE CASCADE,
    naam                TEXT NOT NULL,
    gebiedsaanwijzing_id TEXT NULL REFERENCES gebiedsaanwijzing(identificatie),
    norm_id             TEXT NULL REFERENCES norm(identificatie),
    activiteit_id       TEXT NULL REFERENCES activiteit(identificatie)
);

-- =============================================================
-- 5. IMTR — Toepasbare regels
-- =============================================================

CREATE TABLE IF NOT EXISTS regelbeheerobject (
    functionele_structuur_ref TEXT PRIMARY KEY,
    activiteit_id       TEXT NULL REFERENCES activiteit(identificatie),
    naam                TEXT NULL
);

CREATE TABLE IF NOT EXISTS toepasbaar_regelbestand (
    namespace           TEXT PRIMARY KEY,
    naam                TEXT NULL,
    sttr_versie         INT NOT NULL DEFAULT 1,
    geldig_begindatum   DATE NULL,
    geldig_einddatum    DATE NULL,
    regelbeheerobject   TEXT NULL REFERENCES regelbeheerobject(functionele_structuur_ref)
);

CREATE TABLE IF NOT EXISTS dmn_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regelbestand_ns     TEXT NOT NULL REFERENCES toepasbaar_regelbestand(namespace) ON DELETE CASCADE,
    dmn_id              TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    naam                TEXT NULL,
    parent_id           BIGINT NULL REFERENCES dmn_element(id),
    UNIQUE (regelbestand_ns, dmn_id)
);

CREATE TABLE IF NOT EXISTS uitvoeringsregel (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regelbestand_ns     TEXT NOT NULL REFERENCES toepasbaar_regelbestand(namespace) ON DELETE CASCADE,
    regel_type          TEXT NOT NULL,
    dmn_element_id      BIGINT NULL REFERENCES dmn_element(id),
    nen3610_id          TEXT NULL,
    activiteit_urn      TEXT NULL
);

CREATE TABLE IF NOT EXISTS werkzaamheid (
    urn                 TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    activiteit_id       TEXT NULL REFERENCES activiteit(identificatie)
);

CREATE TABLE IF NOT EXISTS aansluitpunt (
    uri                 TEXT PRIMARY KEY,
    type                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aansluiting (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    aansluitpunt_uri    TEXT NOT NULL REFERENCES aansluitpunt(uri) ON DELETE CASCADE,
    activiteit_id       TEXT NULL REFERENCES activiteit(identificatie),
    bronhouder          TEXT NULL REFERENCES bronhouder(overheidscode),
    regelbestand_ns     TEXT NULL REFERENCES toepasbaar_regelbestand(namespace)
);

-- =============================================================
-- 6. Wro/IMRO — Het oude regime
-- =============================================================

CREATE TABLE IF NOT EXISTS wro_manifest (
    overheidscode       TEXT PRIMARY KEY REFERENCES bronhouder(overheidscode),
    naam_overheid       TEXT NOT NULL,
    datum               DATE NULL
);

CREATE TABLE IF NOT EXISTS wro_dossier (
    dossiernummer       TEXT PRIMARY KEY,
    manifest_code       TEXT NOT NULL REFERENCES wro_manifest(overheidscode) ON DELETE CASCADE,
    status              TEXT NULL REFERENCES dossierstatus(code)
);

CREATE TABLE IF NOT EXISTS ruimtelijk_instrument (
    idn                 TEXT PRIMARY KEY,
    dossier             TEXT NULL REFERENCES wro_dossier(dossiernummer),
    type_plan           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    planstatus          TEXT NULL REFERENCES planstatus(code),
    datum               DATE NULL,
    bronhouder          TEXT NOT NULL REFERENCES bronhouder(overheidscode),
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    pons_status         TEXT NOT NULL DEFAULT 'actief'
);
CREATE INDEX IF NOT EXISTS idx_wro_instrument_geom ON ruimtelijk_instrument USING GIST(geometrie);
CREATE INDEX IF NOT EXISTS idx_wro_instrument_bronhouder ON ruimtelijk_instrument(bronhouder);

CREATE TABLE IF NOT EXISTS planobject (
    identificatie       TEXT PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES ruimtelijk_instrument(idn) ON DELETE CASCADE,
    object_type         TEXT NOT NULL,
    naam                TEXT NULL,
    bestemmingshoofdgroep TEXT NULL REFERENCES bestemmingshoofdgroep(code),
    artikelnummer       TEXT NULL,
    bouwaanduidingtype  TEXT NULL REFERENCES bouwaanduidingtype(code),
    maatvoering_info    JSONB NULL,
    figuurtype          TEXT NULL REFERENCES figuurtype(code),
    gebiedsaanduidinghoofdgroep TEXT NULL REFERENCES gebiedsaanduidinghoofdgroep(code),
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    gml_source          TEXT NULL,
    specificeert_id     TEXT NULL REFERENCES planobject(identificatie)
);
CREATE INDEX IF NOT EXISTS idx_planobject_instrument ON planobject(instrument_idn);
CREATE INDEX IF NOT EXISTS idx_planobject_geom ON planobject USING GIST(geometrie);

CREATE TABLE IF NOT EXISTS wro_tekst_object (
    identificatie       TEXT PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES ruimtelijk_instrument(idn) ON DELETE CASCADE,
    volgnummer          INT NOT NULL,
    niveau              INT NOT NULL CHECK (niveau BETWEEN 0 AND 11),
    parent_id           TEXT NULL REFERENCES wro_tekst_object(identificatie),
    object_type         TEXT NOT NULL,
    label               TEXT NULL,
    nummer              TEXT NULL,
    naam                TEXT NULL,
    inhoud              TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_wro_tekst_instrument ON wro_tekst_object(instrument_idn);

CREATE TABLE IF NOT EXISTS wro_geleideformulier (
    instrument_idn      TEXT PRIMARY KEY REFERENCES ruimtelijk_instrument(idn) ON DELETE CASCADE,
    versie_imro         TEXT NOT NULL,
    versie_praktijkrichtlijn TEXT NOT NULL,
    datum               DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS wro_bronbestand (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_idn      TEXT NOT NULL REFERENCES ruimtelijk_instrument(idn) ON DELETE CASCADE,
    bestandsnaam        TEXT NOT NULL,
    bestandstype        TEXT NOT NULL,
    lettercode          TEXT NULL,
    UNIQUE (instrument_idn, bestandsnaam)
);
"""

LOOKUPS = """
SET search_path TO dso, public;

INSERT INTO bestemmingshoofdgroep (code) VALUES
('Agrarisch'),('Agrarisch met waarden'),('Bedrijf'),('Bedrijventerrein'),
('Bos'),('Centrum'),('Cultuur en ontspanning'),('Detailhandel'),
('Dienstverlening'),('Gemengd'),('Groen'),('Horeca'),('Kantoor'),
('Maatschappelijk'),('Natuur'),('Recreatie'),('Sport'),('Tuin'),
('Verkeer'),('Water'),('Wonen'),('Woongebied'),('Overig')
ON CONFLICT DO NOTHING;

INSERT INTO dubbelbestemmingshoofdgroep (code) VALUES
('Leiding'),('Waarde'),('Waterstaat')
ON CONFLICT DO NOTHING;

INSERT INTO bouwaanduidingtype (code) VALUES
('aaneengebouwd'),('antennemast'),('bijgebouwen'),('gestapeld'),
('kap'),('karakteristiek'),('nokrichting'),('onderdoorgang'),
('plat dak'),('twee-aaneen'),('vrijstaand'),('specifieke bouwaanduiding')
ON CONFLICT DO NOTHING;

INSERT INTO figuurtype (code) VALUES
('as van de weg'),('dwarsprofiel'),('gevellijn'),('hartlijn leiding'),
('hartlijn leiding - brandstof'),('hartlijn leiding - gas'),
('hartlijn leiding - hoogspanning'),('hartlijn leiding - hoogspanningsverbinding'),
('hartlijn leiding - olie'),('hartlijn leiding - riool'),
('hartlijn leiding - water'),('relatie')
ON CONFLICT DO NOTHING;

INSERT INTO gebiedsaanduidinghoofdgroep (code) VALUES
('geluidzone'),('luchtvaartverkeerzone'),('milieuzone'),
('reconstructiewetzone'),('veiligheidszone'),('vrijwaringszone'),
('wetgevingzone'),('overige zone')
ON CONFLICT DO NOTHING;

INSERT INTO dossierstatus (code) VALUES
('in voorbereiding'),('vastgesteld'),('geheel in werking'),
('deels in werking'),('niet in werking'),
('geheel onherroepelijk in werking'),('deels onherroepelijk in werking'),
('vervallen'),('geconsolideerd')
ON CONFLICT DO NOTHING;

INSERT INTO planstatus (code) VALUES
('concept'),('voorontwerp'),('ontwerp'),('vastgesteld'),('geconsolideerd')
ON CONFLICT DO NOTHING;

INSERT INTO regelingmodel (code) VALUES
('RegelingKlassiek'),('RegelingCompact'),
('RegelingTijdelijkdeel'),('RegelingVrijetekst')
ON CONFLICT DO NOTHING;

INSERT INTO besluitmodel (code) VALUES
('BesluitKlassiek'),('BesluitCompact')
ON CONFLICT DO NOTHING;

INSERT INTO publicatiebladtype (code) VALUES
('Staatsblad'),('Staatscourant'),('Gemeenteblad'),
('Provinciaalblad'),('Waterschapsblad'),('BladGR')
ON CONFLICT DO NOTHING;

INSERT INTO idealisatie (code) VALUES
('exact'),('indicatief')
ON CONFLICT DO NOTHING;

INSERT INTO toestemmingstype (code) VALUES
('Vergunningplicht'),('Meldingsplicht'),('Informatieplicht'),
('Verbod'),('Gebod'),('Toegestaan'),('Anders geduid')
ON CONFLICT DO NOTHING;

INSERT INTO documenttype (code) VALUES
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
