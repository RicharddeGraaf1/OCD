-- KOOP omgevingsvergunning-kennisgevingen — losstaand schema in OCD-DB.
--
-- LET OP: Sinds 2026-05-31 leeft de canonical KOOP-DDL in
--   src/ddl.py:KOOP_DDL
-- en kan worden toegepast via `python -m src.cli setup-koop` (of als
-- onderdeel van het algemene `python -m src.cli setup`). Dit bestand
-- blijft bestaan voor backwards-compat met `python ingest.py setup`
-- en moet inhoudelijk gelijk blijven aan KOOP_DDL.
--
-- Bewust geen FK's naar dso.bevoegd_gezag of andere tabellen. De loader
-- haalt records uit KOOP SRU en persisteert ze hier zonder verdere
-- koppeling. Activiteit (KOOP-waardelijst) en bg_naam (vrije vorm)
-- blijven tekstwaarden; eventuele mapping naar IMOW-Activiteit en
-- bestaande BevoegdGezag-tabel is een latere keuze.
--
-- Achtergrond: zie vault_v1/analysis/Ingest omgevingsvergunningen
-- uit officielebekendmakingen.md en vault_v1/model.md §14.

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
-- Houdt `python ingest.py setup` veilig om herhaaldelijk te draaien.
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
-- van hosts staat in deeplinks.py en wordt periodiek uitgebreid op basis van
-- nieuwe deeplink-host-analyse.
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
