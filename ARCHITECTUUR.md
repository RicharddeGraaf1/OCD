# OCD Architectuur

**Datum:** 2026-05-04

---

## Wat is OCD?

OCD (Omgevingswet Centraal Datamodel) is een lokale Postgres+PostGIS
database die alle regelgeving uit het DSO (Digitaal Stelsel Omgevingswet)
samenbrengt in één querybaar datamodel. Snapshot-only, read-only, alle
bronhouders.

De database is opgedeeld in keten-gedreven schema's die de DSO-ketenlogica
weerspiegelen. Daaromheen draaien meerdere tools die de data consumeren.

---

## Totaaloverzicht

```
                        ┌─────────────────────┐
                        │    DSO-API's         │
                        │  Presenteren v8      │
                        │  RTR v2 / STTR v1    │
                        │  Download v1         │
                        └────────┬────────────┘
                                 │
                        ┌────────▼────────────┐
                        │  PDOK               │
                        │  Ruimtelijke Plannen │
                        │  Locatieserver       │
                        │  Kadastrale Kaart    │
                        └────────┬────────────┘
                                 │
          ┌──────────────────────▼──────────────────────┐
          │            dso-loader                        │
          │  src/pipeline/                               │
          │    core.py  → schema's + lookups             │
          │    p2p.py   → Ow via api_loader              │
          │    wro.py   → Wro via PDOK + IHR             │
          │    i2a.py   → IMTR via RTR/STTR              │
          └──────────────────────┬──────────────────────┘
                                 │
     ┌───────────────────────────▼───────────────────────────┐
     │                OCD Database (Postgres + PostGIS)       │
     │                                                        │
     │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │
     │  │   core   │ │   p2p    │ │   wro    │ │   i2a    │ │
     │  │ 16 tbl   │ │ 23 tbl   │ │  7 tbl   │ │  7 tbl   │ │
     │  │waardelijst│ │regelingen│ │bestemming│ │toepasbare│ │
     │  │bronhouder│ │OW-object │ │planobject│ │DMN/STTR  │ │
     │  └──────────┘ └──────────┘ └──────────┘ └──────────┘ │
     │                                                        │
     │  ┌──────────┐ ┌──────────┐ ┌──────────────┐          │
     │  │   v2a    │ │   conv   │ │ p2pwijziging │          │
     │  │  0 tbl   │ │ 13 tbl   │ │  5 tbl + 2 v │          │
     │  │vergunning│ │bp → op   │ │ontwerpen +   │          │
     │  │(reserved)│ │conversie │ │besluitversies│          │
     │  └──────────┘ └──────────┘ └──────────────┘          │
     └───────┬──────────┬──────────┬──────────┬──────────────┘
             │          │          │          │
    ┌────────▼───┐ ┌────▼─────┐ ┌─▼────────┐ │
    │  ocd-api   │ │omgevings-│ │odkwaliteit│ │
    │  FastAPI   │ │bot.nl    │ │annotatie- │ │
    │  /v1/adres │ │RAG + LLM │ │conformit. │ │
    │  /v1/zoek  │ │          │ │36+6 rules │
    │  /v1/...   │ │          │ │           │ │
    └────────────┘ └──────────┘ └───────────┘ │
                                              │
             ┌────────────────────────────────┘
             │
    ┌────────▼───────────┐    ┌──────────────────────┐
    │ bp-converter       │    │ toepasbare-regel-    │
    │ wro.* → conv.*     │    │ checker              │
    │ stap 1: mechanisch │    │ i2a.dmn vs.          │
    │ stap 2: LLM        │    │ p2p.tekst_element    │
    │ stap 3: review     │    │ LLM-vergelijking     │
    └────────────────────┘    └──────────────────────┘
```

---

## Schema's

### `core` — referentiegegevens (16 tabellen)

Waardelijsten en stamgegevens die door alle ketens geconsumeerd worden.
Geen keten-eigenaar.

**Tabellen:** bronhouder, waardelijst, bestemmingshoofdgroep,
dubbelbestemmingshoofdgroep, bouwaanduidingtype, maatvoeringsaanduiding,
figuurtype, gebiedsaanduidinghoofdgroep, dossierstatus, planstatus,
regelingmodel, besluitmodel, publicatiebladtype, idealisatie,
toestemmingstype, documenttype.

### `p2p` — plan-tot-publicatie (23 tabellen)

Het Ow-regime: STOP-regelingen, besluiten en CIM-OW annotaties
(activiteiten, locaties, gebiedsaanwijzingen, normen, juridische regels).

**STOP (7):** regeling, besluit, besluit_regeling, procedurestap,
tekst_element, geo_informatieobject, juridische_borging.

**CIM-OW (16):** locatie, locatiegroep_lid, juridische_regel, activiteit,
activiteit_locatieaanduiding, gebiedsaanwijzing,
juridische_regel_gebiedsaanwijzing, norm, normwaarde,
juridische_regel_norm, tekstdeel, hoofdlijn, tekstdeel_hoofdlijn,
pons, kaart, kaartlaag.

### `wro` — oud regime (7 tabellen)

Wro/IMRO bestemmingsplannen. Eigen technische stack, eigen loaders.
Sunset 2032 — als alle bestemmingsplannen zijn omgezet naar
omgevingsplannen wordt dit schema irrelevant (`DROP SCHEMA wro CASCADE`).

**Tabellen:** wro_manifest, wro_dossier, ruimtelijk_instrument,
planobject, wro_tekst_object, wro_geleideformulier, wro_bronbestand.

### `i2a` — idee-tot-afhandeling (7 tabellen)

Toepasbare regels (STTR/IMTR), werkzaamhedencatalogus en
aansluitpunten. De keten die de DSO-vergunningcheck aandrijft:
werkzaamheid → activiteit → regelbeheerobject → DMN-beslislogica.

**Tabellen:** regelbeheerobject, toepasbaar_regelbestand, dmn_element,
uitvoeringsregel, werkzaamheid, aansluitpunt, aansluiting.

### `v2a` — vraag-tot-antwoord (0 tabellen, gereserveerd)

Gereserveerd voor:
- Vergunningen (scraping van officielebekendmakingen.nl)
- Zoekindex-caches (pgvector embeddings, full-text materialized views)
- Viewer-gerichte aggregaties

### `conv` — conversie-output (13 tabellen)

Bestemmingsplan → omgevingsplan conversie. Afgeleid uit `wro`,
herhaalbaar (wis en opnieuw draaien). Zelfde tabelstructuur als `p2p`
zodat dezelfde queries werken, maar gescheiden om autoritatieve data
en conversie-voorstellen niet te mengen.

**Eigen tabellen:** conversie_meta (bron-instrument, stap, bron-type,
timestamp, LLM-model).

**p2p-equivalent tabellen:** regeling, tekst_element, locatie,
locatiegroep_lid, gebiedsaanwijzing, activiteit, juridische_regel,
activiteit_locatieaanduiding, juridische_regel_gebiedsaanwijzing,
norm, normwaarde, juridische_regel_norm.

Elke tabel heeft een `bron`-kolom: `'mechanisch'` (stap 1) of
`'llm-voorstel'` (stap 2).

### `p2pwijziging` — aankomende wijzigingen (5 tabellen + 2 views)

Ontwerpen en besluitversies die wijzigen wat in `p2p` staat.
Delta-gebaseerd: alleen de annotaties/teksten/locaties die
toegevoegd, gewijzigd of verwijderd worden t.o.v. de geconsolideerde
versie. Eén `besluit`-tabel met `soort`-discriminator
(`'ontwerp' | 'besluitversie'`); de delta-tabellen zijn voor beide
soorten identiek.

**Tabellen:**
- `besluit` — metadata (work, expression, status, inwerkingtreding, …)
- `procedurestap` — bekendmaking, vaststelling, ondertekening, …
- `tekst_element` — volledige documentstructuur als boom (mirror van `p2p.tekst_element`) met renvooi-attributen `wijzigactie` (voegtoe / verwijder / nieuweContainer / verwijderContainer), `vervallen`, `bevat_renvooi` op gewijzigde nodes
- `annotatie_delta` — gewijzigde IMOW-annotaties (JSONB payload)
- `locatie_delta` — gewijzigde geometrieën (PostGIS, optioneel ingeladen)

**Views:**
- `ontwerp` — `SELECT * FROM besluit WHERE soort = 'ontwerp'`
- `besluitversie` — `SELECT * FROM besluit WHERE soort = 'besluitversie'`

**Filter-logica** (alleen écht aankomende wijzigingen):
1. Wij moeten de regeling kennen (`p2p.regeling.frbr_work` matcht)
2. De wijziging introduceert een **andere expression** dan onze huidige
3. Voor besluitversies: `begin_inwerking >= vandaag`
4. Voor ontwerpen: `bekend_op >= datum_van_huidige_p2p_versie`

Zie `docs/p2pwijziging.md`.

---

## Schema-afhankelijkheden

```
core ◄─── p2p           (FK's: bronhouder, regelingmodel, documenttype,
                          besluitmodel, idealisatie)
core ◄─── wro           (FK's: bronhouder, planstatus, dossierstatus,
                          bestemmingshoofdgroep, bouwaanduidingtype, figuurtype,
                          gebiedsaanduidinghoofdgroep)
core ◄─── i2a           (FK: aansluiting.bronhouder)
core ◄─── conv          (FK: regeling.bronhouder)
core ◄─── p2pwijziging  (FK: besluit.bronhouder)
p2p  ◄─── i2a           (FK's: regelbeheerobject.activiteit_id,
                          werkzaamheid.activiteit_id,
                          aansluiting.activiteit_id)
p2p  ───► p2pwijziging  (filter-referentie: regeling_work +
                          frbr_expression bepalen relevantie, geen FK)
wro  ───► conv          (bron-data voor conversie, geen FK)
p2p  ───► conv          (referentie voor bruidsschat-conflictdetectie, geen FK)
```

**Richting:** `core` is de basis, `p2p`/`wro` leunen erop, `i2a` leunt op
`p2p` + `core`, `conv` leunt op `core` en leest uit `wro` + `p2p`.

---

## Dataflow per schema

```
DSO Presenteren v8 ──────────────► p2p          (Ow-regelingen, STOP + CIM-OW objecten)
DSO Presenteren v8 ontwerpen ────► p2pwijziging (ontwerpregelingen + besluitversies)
DSO Presenteren v8 besluitversies ► p2pwijziging
PDOK + IHR ──────────────────────► wro          (bestemmingsplannen, planobjecten, teksten)
DSO RTR + STTR ──────────────────► i2a          (toepasbare regels, DMN, werkzaamheden)
Lookups + bronhouder ────────────► core         (waardelijsten, stamgegevens)
(gepland) OB-scraper ────────────► v2a          (vergunningen uit officielebekendmakingen.nl)
bp-converter ────────────────────► conv         (wro mechanisch + LLM omgezet naar Ow-structuur)
```

---

## Componenten

### dso-loader (`C:/GIT/OCD/dso-loader/`)

Python-package dat data uit DSO-API's en PDOK laadt in OCD.

**Pipeline** (`src/pipeline/`):
```bash
python -m src.cli pipeline core                    # DDL + lookups
python -m src.cli pipeline p2p  -f gemeenten.json  # Ow-regelingen
python -m src.cli pipeline wro  -f gemeenten.json  # Wro-plannen + teksten
python -m src.cli pipeline i2a  -f gemeenten.json  # IMTR
python -m src.cli pipeline all  -f gemeenten.json  # Alles in volgorde
```

**Wijzigingen** (`src/loaders/ontwerp_loader.py`):
```bash
python -m src.cli wijziging ontwerpen   # Ontwerpregelingen via Presenteren v8
python -m src.cli wijziging besluiten   # Besluitversies via Presenteren v8
python -m src.cli wijziging status      # Overzicht
```

**Refresh** (`scripts/refresh_p2p_expressions.py`):
```bash
python scripts/refresh_p2p_expressions.py  # Houd p2p actueel zodat
                                           # p2pwijziging-filter klopt
```

**Queries** (`src/query.py`):
```bash
python -m src.cli adres Keizersgracht 100 Amsterdam
python -m src.cli zoek dakkapel
python -m src.cli activiteiten 0344
python -m src.cli status
```

**Loaders:** `src/loaders/api_loader.py` (Ow via Presenteren),
`src/loaders/imtr_loader.py` (IMTR), `src/loaders/wro_pdok.py` (Wro via PDOK),
`src/loaders/ihr_loader.py` (Wro-teksten via IHR).

### ocd-api (`C:/GIT/OCD/ocd-api/`)

FastAPI REST-service bovenop OCD. Endpoints:
- `GET /v1/adres?q=...` — wat geldt op een adres (cross-regime)
- `GET /v1/zoek?q=...` — full-text search Ow + Wro
- `GET /v1/gemeente/{code}/activiteiten` — alle activiteiten
- `GET /v1/gemeente/{code}/normen` — alle normen
- `GET /v1/gemeente/{code}/pons` — pons-status
- `GET /v1/gezagen` — bevoegde gezagen met laad-status
- `GET /v1/overzicht` — row counts per tabel

### omgevingsbot.nl (`C:/GIT/omgevingsbot.nl/`)

RAG-pipeline (Retrieval-Augmented Generation) die vragen over de
Omgevingswet beantwoordt. Kan OCD als primaire bron gebruiken in plaats
van directe DSO-API calls (zie `docs/optimalisaties.md`).

**Geplande verbeteringen:**
- OCD als databron (1 SQL i.p.v. 5-10 API-calls)
- IMTR-retrieval-path via i2a-tabellen
- Wro-teksten altijd ophalen (niet conditioneel op PONS)

### odkwaliteit (`C:/GIT/annotatieconformiteit.nl/`)

Annotatieconformiteit-scorer achter [annotatieconformiteit.nl](https://annotatieconformiteit.nl):
**45 richtlijnen** — de 36 Geonovum-richtlijnen (cat. A–F; F is in v1.0 leeg) plus
projecteigen cat. G (R37–R43, toepasbare regels) en H (R44–R45, ontbrekende
referenties). Per omgevingsdocument een conformiteitsscore (gewogen A–F) en een
uitgebreide score (A–H, met G 12% en H 8%), plus een rollup per bevoegd gezag.
Stand 2026-07-30: 1597 regelingen, 363 bevoegde gezagen.

**OCD-integratie is gerealiseerd** (`docs/plan-ocd-integratie.md`):
- OCD-collector is de standaardbron (`collect --source ocd`, default); de
  DSO-API-collector bestaat nog als alternatief. Volledige run duurt minuten
  i.p.v. uren.
- Cat. G en H draaien, inclusief G43 (dangling toepasbare regels per bronhouder)
  en H44/H45 op de drieslag-matviews `tekst_object_consistentie_mv` /
  `gio_referentie_consistentie_mv`.
- R11 is geautomatiseerd op `p2p.tekst_element.inhoud` — een tekstheuristiek,
  géén LLM (er zit nergens een LLM in de scorer).

**Nog niet af:**
- R13 is nog 100% handmatig — 1597 van 1597 bevindingen staan op "niet getoetst".
- R36 (overlap-detectie via PostGIS) zit wél in de collector
  (`collect_norm_overlaps`), maar staat uit in de pijplijn:
  `ocd_orchestrator` zet `norm_overlaps = []` met de comment "PostGIS overlap is
  slow". Gevolg: R36 meldt voor alle 52 regelingen met normen "geen overlap
  gevonden" = voldoet, zonder dat de check ooit liep — een gratis pass.
- R44 is voor 1468 van 1597 regelingen "niet getoetst" omdat
  `naamInformatieObject` nog niet in OCD is geladen (loader + GIO-tak in de
  naam-match vereist).

### bp-converter (gepland)

Bestemmingsplan → omgevingsplan conversie in drie stappen:

| Stap | Input | Output | Methode |
|---|---|---|---|
| 1 | wro.* | conv.regeling, conv.tekst_element, conv.locatie, conv.gebiedsaanwijzing | Mechanisch (SQL) |
| 2 | conv.tekst_element + p2p.activiteit (context) | conv.activiteit, conv.norm, conv.juridische_regel | LLM-ondersteund |
| 3 | conv.* | gevalideerd conv.* | Menselijke review |

Zie `docs/bestemmingsplan-converter.md`.

### toepasbare-regel-checker (gepland)

Vergelijkt DMN-beslislogica (i2a) met artikeltekst (p2p) per activiteit.
Detecteert ontbrekende condities, verkeerde drempels, foutieve conclusies.

Output: kwaliteitsrapport per gemeente. Integreert als categorie G in
odkwaliteit.

Zie `docs/toepasbare-regel-checker.md`.

---

## Database-omvang (snapshot 2026-05-04)

| Schema | Tabel | Rijen |
|---|---|---|
| core | bronhouder | 399 |
| p2p | regeling | 1.868 |
| p2p | tekst_element | 614.128 |
| p2p | juridische_regel | 260.177 |
| p2p | activiteit | 33.627 |
| p2p | activiteit_locatieaanduiding | 266.983 |
| p2p | locatie | 27.338 |
| p2p | normwaarde | 18.289 |
| wro | ruimtelijk_instrument | 55.085 |
| wro | planobject | 5.999.982 |
| wro | wro_tekst_object | 795.247 |
| i2a | toepasbaar_regelbestand | 53.379 |
| i2a | dmn_element | 930.123 |
| i2a | uitvoeringsregel | 388.887 |
| p2pwijziging | besluit | 214 (198 ontwerp + 16 besluitversie) |
| p2pwijziging | annotatie_delta | 363.231 |
| p2pwijziging | locatie_delta | 1.083.472 |
| conv | regeling | 28.801 |
| conv | tekst_element | 438.039 |
| conv | locatie | 3.256.700 |
| conv | activiteit | 816.773 (matcher) + 2.944 (LLM) |
| | **Totaal** | **~24.500.000** |

---

## Technische stack

| Component | Technologie |
|---|---|
| Database | PostgreSQL 16 + PostGIS 3.4 (Docker: `dso-postgis`) |
| API | Python 3.13 + FastAPI |
| Loaders | Python 3.13 + httpx + psycopg 3 |
| Omgevingsbot | Python + FastAPI + Groq/Ollama LLM |
| odkwaliteit | Python 3.12 + httpx async + SQLAlchemy + Next.js frontend |
| CLI | Click + Rich |

---

## Documentatie-index

| Document | Locatie | Beschrijving |
|---|---|---|
| Schema-indeling | `OCD/SCHEMA-INDELING.md` | Onderbouwing keten-schema's, tabel-toewijzing |
| Migratiescript | `OCD/dso-loader/scripts/migrate_to_keten_schemas.sql` | dso → core/p2p/wro/i2a migratie |
| Bestemmingsplan-converter | `OCD/docs/bestemmingsplan-converter.md` | 3-staps conversie-ontwerp met conv-schema |
| Toepasbare-regel-checker | `OCD/docs/toepasbare-regel-checker.md` | DMN vs. artikeltekst vergelijking |
| p2pwijziging-ontwerp | `OCD/docs/p2pwijziging.md` | Ontwerpen + besluitversies, delta-opslag, filter-logica |
| OCD-integratie odkwaliteit | `odkwaliteit/docs/plan-ocd-integratie.md` | OCD als databron voor annotatieconformiteit |
| Omgevingsbot optimalisaties | `omgevingsbot.nl/docs/optimalisaties.md` | 14 verbeterpunten voor RAG-pipeline |
| Ideeën | `vault_v1/ideeen.md` | 8 productideeën met haalbaarheid |
