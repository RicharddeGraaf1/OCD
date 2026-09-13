# Dekkingsmatrix — één artefact, twee afnemers

**Status**: ontwerpnotitie · **Datum**: 2026-08-13
**Aanleiding**: bronroutering in omgevingsbot.nl (welke van de drie bronnen kan
deze vraag dragen) + het idee van een dekkingskaart per locatie.

---

## 1. Waarom dit één ding is

De bot heeft drie bronnen — `p2p` (Ow-omgevingsdocumenten), `wro`
(IMRO-bestemmingsplannen) en `i2a` (toepasbare regels) — en weet vooraf niet
waar het antwoord staat. Vandaag lost hij dat op met een keten van pogingen:
Ow eerst, Wro als fallback, en achteraf een coverage-veto.

- `/v1/coverage` ([main.py:1286](../ocd-api/main.py#L1286)) telt Ow-regels en
  RO-planobjecten op een punt en geeft een binair `has_rules`. `i2a` ontbreekt.
  Het `onderwerp`-filter is een `ILIKE` op naam-velden.
- De bot roept dat aan **ná** de LLM-call
  ([ocd.py:1948-1988](../../omgevingsbot.nl/backend/services/pipelines/ocd.py#L1948-L1988)),
  als veto op een antwoord dat al gegenereerd is.
- De Wro-maatvoering is een *fallback*
  ([ocd.py:1626](../../omgevingsbot.nl/backend/services/pipelines/ocd.py#L1626)),
  terwijl eerder gemeten is dat 296 van de 342 gemeenten nul Ow-normen heeft.
  Voor "hoe hoog mag dit" is Wro daar het primaire pad, niet het vangnet.

De dekkingskaart die we willen tonen en de routeringsbeslissing die de bot moet
nemen zijn dezelfde vraag: *wat is hier, over dit onderwerp, in welke bron
geregeld — en als er niets is, weten we dan waarom?* Daarom één matrix, twee
afnemers. De bot leest hem als beslissing vóór de retrieval, de kaart kleurt hem.

## 2. Wat er al ligt

| Onderdeel | Waar | Bruikbaar als |
|---|---|---|
| Onderwerp-as | `v2a.artikel_indeling` (live 2026-08-09) | categorie / subcategorie / type_bepaling per artikel |
| Curatie-sleutel | `v2a.pad_categorie` | genormaliseerd voorouderpad → categorie |
| Tekst-as over bronnen heen | `v2a.chunk` (view op `tekst_embedding`) | p2p **en** wro, `source_type` als discriminator |
| Geometrie op drie niveaus | `p2p.locatie_generalisatie` | tegels voor de kaart |
| Bronhouder-gezondheid | `core.mv_bronhouder_health`, `/v1/data-health` | signaal "wij hebben iets gemist" |
| Laadverantwoording | `p2p.regeling_load`, `core.load_run` | idem |

Er hoeft dus weinig nieuw gemeten te worden. Wat ontbreekt is de *samenvoeging*
en — belangrijker — de discipline in wat je telt.

## 3. Drie correcties, anders telt de matrix niets

### 3.1 De bruidsschat eraf

De bruidsschat staat woordelijk identiek in ~340 gemeenten. Een dekkingsgetal
dat hem meetelt kleurt heel Nederland groen op bodem, geluid en procedures en
onderscheidt niets. Twee kolommen dus:

- `n_artikelen` — wat hier geldt (inclusief bruidsschat).
- `n_eigen` — wat deze bronhouder zélf heeft geregeld.

De scheiding is goedkoop: de ijkmeting van de onderwerp-as ontdubbelt al op
kale tekst, precies om te voorkomen dat je meet hoe vaak de bruidsschat
gekopieerd is. Dezelfde hash-dedup levert hier `n_eigen`.

### 3.2 De typeBepaling-as eraf

~26% van de toewijzingen zit in zuivere typeBepaling-categorieën, met de
mengvormen erbij ~40%. "150 artikelen over geur" waarvan driekwart de zin
"deze paragraaf is (niet) van toepassing" is, is geen dekking.
`artikel_indeling.type_bepaling` staat er al; `n_normatief` telt alleen wat
overblijft als je toepassingsbereik, oogmerk en procedurele bepalingen
wegstreept.

### 3.3 Drie soorten leeg mogen niet dezelfde kleur krijgen

Dit is de kern. Nul kan drie dingen betekenen:

| Reden | Betekenis | Van wie is het |
|---|---|---|
| `niet-geregeld` | de bronhouder heeft hier niets over bepaald | legitiem leeg |
| `niet-geannoteerd` | het staat er wel, maar draagt geen object dat we kunnen vinden | bronhouder, andere boodschap |
| `pipeline` | wij hebben het gemist of niet ingedeeld | **onze schuld** |

G-127 laat zien dat het derde vak geen theorie is: de nieuwe planversies halen
71,2% ingedeeld tegen 84,7% landelijk, met Epe op 49,6% en Ede op 51,6% — niet
omdat die gemeenten minder regelen, maar omdat hun opschriftpaden nog niet
gecureerd zijn. Een kaart die daarop leunt kleurt Ede rood voor een probleem
dat van ons is.

**Beslisregel, in deze volgorde** (bij twijfel is het onze schuld):

1. `n_artikelen > 0` → gedekt, geen reden nodig.
2. Anders, en `n_niet_ingedeeld` boven drempel in dezelfde regeling → `pipeline`.
3. Anders, en `regeling_load` / `mv_bronhouder_health` meldt onvolledig geladen
   → `pipeline`.
4. Anders, en de tekst-as (`v2a.chunk`) vindt in deze regeling wél chunks die
   op dit onderwerp classificeren → `niet-geannoteerd`.
5. Anders → `niet-geregeld`.

Regel 4 is precies de rapportage waar annotatieconformiteit voor bestaat: "u
heeft het geregeld, maar niet zo geannoteerd dat een afnemer het vindt."

## 4. DDL-voorstel

Twee tabellen: één ijl op locatieniveau (de kaart), één dicht op
bronhouderniveau (de router en het oordeel over leeg). Voorstel voor
`scripts/2026-08-add-dekking.sql`.

```sql
-- ============================================================================
-- 2026-08 · Dekkingsmatrix — (scope × bron × categorie) met reden-van-leeg.
--
-- Twee niveaus, bewust gescheiden:
--   dekking_locatie     IJL   — alleen rijen > 0. Voedt de kaart.
--   dekking_bronhouder  DICHT — inclusief de nullen, want juist de nul is
--                               de informatie. Voedt de bot-routering.
--
-- `categorie` is NOT NULL met sentinel: NULL in een PK maakt de opzoeking
-- onbetrouwbaar en verbergt het verschil tussen "geen categorie" en "categorie
-- onbekend". Het sentinel maakt niet-ingedeeld telbaar naast de rest.
-- ============================================================================

CREATE TABLE IF NOT EXISTS v2a.dekking_locatie (
    locatie_id       TEXT NOT NULL
                     REFERENCES p2p.locatie(identificatie) ON DELETE CASCADE,
    bron             TEXT NOT NULL,   -- 'p2p' | 'wro' | 'i2a'
    categorie        TEXT NOT NULL,   -- IMOW-thema of '(niet ingedeeld)'
    n_artikelen      INT  NOT NULL,   -- alles wat hier geldt
    n_eigen          INT  NOT NULL,   -- na aftrek bruidsschat
    n_normatief      INT  NOT NULL,   -- na aftrek typeBepaling-bepalingen
    n_met_norm       INT  NOT NULL,   -- draagt een kwantitatieve normwaarde
    PRIMARY KEY (locatie_id, bron, categorie)
);

CREATE INDEX IF NOT EXISTS dekking_locatie_cat_idx
    ON v2a.dekking_locatie (categorie, bron);

CREATE TABLE IF NOT EXISTS v2a.dekking_bronhouder (
    overheidscode    TEXT NOT NULL REFERENCES core.bronhouder(overheidscode),
    bron             TEXT NOT NULL,
    categorie        TEXT NOT NULL,
    n_artikelen      INT  NOT NULL DEFAULT 0,
    n_eigen          INT  NOT NULL DEFAULT 0,
    n_normatief      INT  NOT NULL DEFAULT 0,
    n_met_norm       INT  NOT NULL DEFAULT 0,
    n_niet_ingedeeld INT  NOT NULL DEFAULT 0,  -- artikelen zonder categorie
    -- NULL = niets aan de hand (er is dekking). Anders de reden van de nul,
    -- volgens de beslisregel in docs/dekkingsmatrix.md §3.3.
    reden_leeg       TEXT CHECK (reden_leeg IN
                       ('niet-geregeld', 'niet-geannoteerd', 'pipeline')),
    -- Waarop de reden gebaseerd is, zodat het oordeel navolgbaar blijft en
    -- niet als kale kleur op een kaart eindigt.
    reden_bewijs     JSONB,
    gemeten_op       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (overheidscode, bron, categorie)
);

CREATE INDEX IF NOT EXISTS dekking_bronhouder_leeg_idx
    ON v2a.dekking_bronhouder (reden_leeg)
    WHERE reden_leeg IS NOT NULL;
```

Ontwerpkeuzes die uitleg verdienen:

- **`reden_bewijs` als JSONB.** Een kaart die gemeenten kleurt moet kunnen
  verantwoorden waarom. Hier komt in te staan welke regel uit §3.3 vuurde en
  met welke tellingen — bijvoorbeeld `{"regel": 2, "n_niet_ingedeeld": 153,
  "n_artikelen_regeling": 316}`. Zonder dit veld is de reden een gok met een
  kleurtje.
- **Geen `reden_leeg` op locatieniveau.** Een locatie zonder rij is gewoon
  afwezig uit de ijle tabel; de reden is een eigenschap van de bronhouder plus
  categorie, niet van elk polygoon apart.
- **`i2a` deelt de categorie-as niet.** Toepasbare regels hangen aan
  activiteiten, niet aan artikelopschriften. Vullen via
  `i2a.uitvoeringsregel` → `activiteit_urn` → `p2p.activiteit` → de categorie
  van de artikelen die die activiteit dragen. Dat is een afgeleide en hoort in
  `reden_bewijs` als zodanig herkenbaar te zijn.
- **`wro` heeft geen artikel-indeling.** Wro-chunks zitten wél in `v2a.chunk`
  (`source_type = 'wro'`) maar hebben geen `artikel_indeling`-rij. Tot die er
  is, krijgt wro per definitie `(niet ingedeeld)` en is `reden_leeg =
  'pipeline'` het eerlijke antwoord — niet `niet-geregeld`.

## 5. Vulling

Een script `scripts/bouw_dekking.py`, in `full_sync` direct **na** de
`indeling`-stap (die vult `artikel_indeling`, waar dit op leunt). Herbouw is
volledig — de tabellen zijn afgeleid en klein genoeg om te wissen en opnieuw te
vullen; incrementeel bijhouden levert alleen scheve standen op.

De stap rapporteert per run minimaal: aantal bronhouders met `reden_leeg =
'pipeline'`, en het verschil met de vorige run. Dat is het signaal dat G-127
had moeten geven en niet gaf.

## 6. Consumptie

**Bot.** Nieuw `/v1/dekking?x=&y=&categorie=`, aangeroepen vóór de retrieval.
Antwoord per bron per categorie, plus `reden_leeg`. Drie uitkomsten:
één bron kan het (ga daarheen), meerdere (parallel, Ow vóór Wro tenzij de
pons-status anders zegt), geen enkele (zeg dat meteen). `/v1/coverage` blijft
staan tot de bot om is.

**Kaart.** Tegels over `p2p.locatie_generalisatie`, gekleurd op
`dekking_locatie`. Twee schakelaars die niet optioneel zijn:

1. *totaal* versus *eigen regels* — zonder de tweede meet je een copy-paste.
2. *reden van leeg* in de legenda, met minstens twee kleuren voor nul:
   "hier is niets geregeld" en "wij weten het niet".

Bijvangst: de taxonomie-gaten worden zichtbaar naast de content-gaten. De
IMOW-ruggengraat heeft `energie = 2`, `mobiliteit = 2`, `economie = 1`
toegewezen chunks. Dat is geen land zonder energieregels, dat is een as die
niet vult — en op een kaart valt dat meteen op.

## 7. Wat dit niet oplost

- **Dekking is geen relevantie.** Dat een gemeente 40 artikelen over bouwen
  heeft zegt niet dat er één tussen zit die de vraag beantwoordt. De matrix
  sluit bronnen uit — goedkoop en zeker — maar wijst er geen aan. De winst zit
  in het uitsluiten.
- **De categorie-as is zelf nog in beweging.** Zolang G-127 open staat, is
  `reden_leeg = 'pipeline'` in een deel van de gevallen het enige eerlijke
  antwoord. Dat is geen bezwaar tegen de matrix; het is precies wat hij hoort
  te laten zien.
- **Publicatie is een aparte beslissing.** Als intern instrument en als
  bronhouder-feedback kan dit meteen mee. Publiceren als "gemeente X regelt
  niets over natuur" vraagt dat §3.1 tot §3.3 waterdicht zijn — anders
  publiceer je je eigen pipeline-gaten als gemeentelijk falen.

## 8. Openstaande keuzes

1. Drempel in beslisregel 2: vanaf welk aandeel `n_niet_ingedeeld` binnen een
   regeling noemen we het `pipeline` in plaats van `niet-geregeld`?
2. Kaart-eenheid voor het landelijke overzicht: uitzoomen naar
   bronhouder-choropleth verbergt juist de variatie binnen een gemeente die de
   kaart interessant maakt. Alternatief is een raster over `dekking_locatie`.
3. Krijgt `wro` een eigen indeling op de tekst-as, of blijft hij
   `(niet ingedeeld)` tot het schema in 2032 vervalt?
