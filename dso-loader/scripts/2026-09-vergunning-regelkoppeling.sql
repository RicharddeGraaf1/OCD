-- Vergunning <-> regel koppelen via de werkzaamheid: fase 0 en 1.
-- Zie docs/vergunning-regelkoppeling-plan.md.
-- Idempotent; draaibaar op een bestaande DB.

-- ---------------------------------------------------------------- fase 0
-- Het bevoegd gezag één keer oplossen, in plaats van in elke query een
-- lower(naam)-join met regexp op core.bronhouder.
--
-- Eerste poging was een kolom op vth.vergunningkennisgeving. Dat is een
-- UPDATE over 925.329 rijen, en die herschrijft elke rij (MVCC) op een
-- database met 128 MB shared_buffers — na een kwartier nog niet klaar.
-- Er zijn maar 387 verschillende (bg_naam, organisatietype)-paren, dus een
-- opzoektabel van 387 rijen doet hetzelfde werk zonder iets te herschrijven.

CREATE TABLE IF NOT EXISTS vth.bg_resolutie (
  bg_naam         TEXT NOT NULL,
  organisatietype TEXT NOT NULL,
  overheidscode   TEXT,          -- NULL = geen bronhouder-tegenhanger (G-158)
  bijgewerkt      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bg_naam, organisatietype)
);

COMMENT ON TABLE vth.bg_resolutie IS
  'KOOP-bevoegdgezagnaam -> core.bronhouder.overheidscode. KOOP schrijft '
  '"Waterschap Hollandse Delta", de bronhouder heet "Hollandse Delta"; zonder '
  'het strippen van dat voorvoegsel blijft het waterschap op 14% steken in '
  'plaats van 82%. overheidscode IS NULL is een geldige uitkomst: '
  'omgevingsdiensten en samenwerkingsorganen publiceren namens een ander '
  'bevoegd gezag en hebben zelf geen bronhouder — zie gaps#G-158.';

INSERT INTO vth.bg_resolutie (bg_naam, organisatietype, overheidscode)
SELECT p.bg_naam, p.organisatietype, b.overheidscode
  FROM (SELECT DISTINCT bg_naam, organisatietype
          FROM vth.vergunningkennisgeving) p
  LEFT JOIN core.bronhouder b
    ON b.bestuurslaag = p.organisatietype
   AND lower(b.naam) = lower(regexp_replace(
         p.bg_naam,
         '^(Waterschap|Hoogheemraadschap( van)?|Wetterskip|Gemeente|Provincie)\s+',
         '', 'i'))
ON CONFLICT (bg_naam, organisatietype) DO UPDATE
   SET overheidscode = EXCLUDED.overheidscode, bijgewerkt = now();

CREATE INDEX IF NOT EXISTS bg_resolutie_overheidscode_idx
  ON vth.bg_resolutie (overheidscode);

-- ---------------------------------------------------------------- fase 1
-- Sla op wat wij produceren: de afgeleide koppeling kennisgeving ->
-- werkzaamheid. Activiteit, regel en regeltekst volgen deterministisch uit
-- gecureerde data die al in de DB staat en worden NIET gematerialiseerd.

CREATE TABLE IF NOT EXISTS vth.vergunning_werkzaamheid (
  koop_id          TEXT     NOT NULL REFERENCES vth.vergunningkennisgeving(koop_id)
                                     ON DELETE CASCADE,
  werkzaamheid_urn TEXT     NOT NULL,
  rang             SMALLINT NOT NULL,
  score            REAL     NOT NULL,
  marge            REAL,              -- afstand tot de volgende kandidaat
  bewijs           TEXT,              -- de frase die matchte
  bewijs_veld      TEXT,
  methode          TEXT     NOT NULL, -- lexicaal | embedding | handmatig
  methode_versie   TEXT     NOT NULL,
  bepaald_op       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (koop_id, werkzaamheid_urn, methode),
  CONSTRAINT vergunning_werkzaamheid_methode_chk
    CHECK (methode IN ('lexicaal', 'embedding', 'handmatig'))
);

CREATE INDEX IF NOT EXISTS vergunning_werkzaamheid_koop_rang_idx
  ON vth.vergunning_werkzaamheid (koop_id, rang);
CREATE INDEX IF NOT EXISTS vergunning_werkzaamheid_urn_idx
  ON vth.vergunning_werkzaamheid (werkzaamheid_urn);

COMMENT ON TABLE vth.vergunning_werkzaamheid IS
  'Afgeleide koppeling kennisgeving -> werkzaamheid. methode scheidt de gok '
  'van het oordeel: een herlaadronde scopet altijd op methode.';

-- De gouden set is een steekproef van VERGUNNINGEN, niet van matcher-uitvoer.
-- Daarom een eigen tabel: een vergunning kan in de set zitten waarvoor geen
-- juiste werkzaamheid bestaat, en dat oordeel is net zo waardevol. Zo blijft
-- de set bruikbaar voor elke volgende matcherversie.
CREATE TABLE IF NOT EXISTS vth.gouden_set (
  koop_id      TEXT PRIMARY KEY REFERENCES vth.vergunningkennisgeving(koop_id)
                                ON DELETE CASCADE,
  stratum      TEXT NOT NULL,
  toegevoegd   TIMESTAMPTZ NOT NULL DEFAULT now(),
  beoordeeld   TIMESTAMPTZ,
  beoordelaar  TEXT,
  oordeel      TEXT,
  juiste_urn   TEXT,
  toelichting  TEXT,
  CONSTRAINT gouden_set_oordeel_chk
    CHECK (oordeel IS NULL OR oordeel IN
           ('juist', 'geen_koppeling_mogelijk', 'onbeoordeelbaar'))
);

COMMENT ON TABLE vth.gouden_set IS
  'Onafhankelijk beoordeelde steekproef met de JUISTE werkzaamheid per '
  'vergunning. Het ijkpunt waartegen elke matcherversie wordt gemeten.';

-- ---------------------------------------------------------------- view
-- De keten na de werkzaamheid. Lui, niet gematerialiseerd: ~17,8 tekstrijen
-- per vergunning is ruim 4 miljoen rijen voor alleen rang 1.
--
-- Twee dingen die niet mogen veranderen:
--   * joinen op tekst_element MOET mee op regeling_expression — wid is niet
--     uniek over regelingen (gaps#G-144);
--   * de annotatie hangt in ~80% aan een Lid zonder opschrift of nummer,
--     dus het artikel erboven komt via parent_id mee.

CREATE OR REPLACE VIEW vth.v_vergunning_regeltekst AS
SELECT vw.koop_id, vw.rang, vw.methode, vw.score, vw.marge, vw.bewijs,
       vw.werkzaamheid_urn, w.naam AS werkzaamheid_naam,
       wa.activiteit_urn, act.naam AS activiteit_naam, act.groep AS activiteit_groep,
       ala.kwalificatie,
       jr.identificatie AS juridische_regel_id,
       te.id AS tekst_element_id, te.element_type, te.eid, te.nummer,
       te.inhoud_plain,
       COALESCE(ouder.nummer, te.nummer)       AS artikel_nummer,
       COALESCE(ouder.opschrift, te.opschrift) AS artikel_opschrift,
       COALESCE(ouder.eid, te.eid)             AS artikel_eid,
       te.regeling_expression, r.citeertitel AS regeling_citeertitel
  FROM vth.vergunning_werkzaamheid vw
  JOIN vth.vergunningkennisgeving v ON v.koop_id = vw.koop_id
  JOIN vth.bg_resolutie bg ON bg.bg_naam = v.bg_naam
                          AND bg.organisatietype = v.organisatietype
  JOIN i2a.werkzaamheid w  ON w.urn = vw.werkzaamheid_urn
  JOIN i2a.werkzaamheid_activiteit wa ON wa.werkzaamheid_urn = vw.werkzaamheid_urn
                                     AND wa.overheid_ns = bg.overheidscode
  JOIN p2p.activiteit act  ON act.identificatie = wa.activiteit_urn
  JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = wa.activiteit_urn
  JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
  JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                           AND te.regeling_expression = jr.regeling_expression
  LEFT JOIN p2p.tekst_element ouder ON ouder.id = te.parent_id
                                   AND ouder.element_type = 'Artikel'
  LEFT JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression;
