# OCD Schema-indeling — keten-gedreven opsplitsing

**Status:** afgestemd, klaar voor implementatie
**Datum:** 2026-04-15
**Scope:** alle tabellen in `src/ddl.py` (DSO Datamodel v1.0)

## Achtergrond

De OCD-database heeft op dit moment één schema (`dso`) waarin alle 46 tabellen
zitten. Dit voorstel splitst die onderverdeling in vijf schema's langs de
DSO-ketenlogica, met Wro als apart historisch schema:

| Schema | Betekenis | Scope |
|--------|-----------|-------|
| `core` | Referentie-/stamgegevens | waardelijsten, bronhouder, gedeelde vocabulaires |
| `p2p`  | Plan-tot-publicatie (Ow) | regelingen, besluiten, OW-objecten |
| `wro`  | Oud regime (Wro/IMRO) | bestemmingsplannen, IMRO-planobjecten, tot sunset 2032 |
| `i2a`  | Idee-tot-afhandeling | toepasbare regels, werkzaamheden, vragenbomen |
| `v2a`  | Vraag-tot-antwoord | viewer-data, later vergunningen, zoekindex-caches |

**Waarom Wro apart:** Wro heeft een eigen technische stack (IMRO/STRI, losse
loaders, eigen manifest-structuur) en een sunset-datum in 2032. Een apart
schema maakt de historische grens expliciet en de uiteindelijke verwijdering
triviaal (`DROP SCHEMA wro CASCADE`).

**Doel:** domein-realiteit zichtbaar maken in de databasestructuur, ownership
expliciet maken, cross-keten queries herkenbaar maken, en per keten
load/refresh-ritme en rechten onafhankelijk kunnen inrichten.

---

## Volledige tabeltoewijzing

### `core` — referentiegegevens (17 tabellen)

Alles dat geen keten-eigenaar heeft: waardelijsten (IMOW/IMRO-vocabulaires)
en de bronhouder-tabel (organisatiekader dat door alle ketens geconsumeerd
wordt).

| Tabel | Nu in | Reden |
|-------|-------|-------|
| `bronhouder` | dso | Organisatiekader, gelezen door p2p + i2a + v2a |
| `waardelijst` | dso | Generieke IMOW-waardelijstentabel |
| `bestemmingshoofdgroep` | dso | IMRO-waardelijst |
| `dubbelbestemmingshoofdgroep` | dso | IMRO-waardelijst |
| `bouwaanduidingtype` | dso | IMRO-waardelijst |
| `maatvoeringsaanduiding` | dso | IMRO-waardelijst |
| `figuurtype` | dso | IMRO-waardelijst |
| `gebiedsaanduidinghoofdgroep` | dso | IMRO-waardelijst |
| `dossierstatus` | dso | IMRO-waardelijst |
| `planstatus` | dso | IMRO-waardelijst |
| `regelingmodel` | dso | STOP-waardelijst |
| `besluitmodel` | dso | STOP-waardelijst |
| `publicatiebladtype` | dso | STOP-waardelijst |
| `idealisatie` | dso | IMOW-waardelijst |
| `toestemmingstype` | dso | IMOW-waardelijst |
| `documenttype` | dso | STOP-waardelijst |

### `p2p` — plan-tot-publicatie Ow (16 tabellen)

Het omgevingsdocument (STOP) en zijn IMOW-annotaties. Alleen het
huidige Ow-regime; Wro heeft zijn eigen schema.

**STOP — Regelingen & Besluiten (7 tabellen):**

| Tabel | Nu in | Reden |
|-------|-------|-------|
| `regeling` | dso | STOP-regeling is het kerninstrument van de planketen |
| `tekst_element` | dso | Onderdeel van de regeling |
| `besluit` | dso | Wijzigt een regeling, hoort bij de planketen |
| `besluit_regeling` | dso | Junction |
| `procedurestap` | dso | Procedurele status van besluit |
| `geo_informatieobject` | dso | GIO hoort bij het omgevingsdocument |
| `juridische_borging` | dso | Koppelt GIO aan regeling |

**CIM-OW — OW-objecten in het omgevingsdocument (12 tabellen):**

| Tabel | Nu in | Reden |
|-------|-------|-------|
| `locatie` | dso | IMOW-locatie, gedefinieerd in omgevingsdocument |
| `locatiegroep_lid` | dso | Locatiegroep-hiërarchie |
| `juridische_regel` | dso | IMOW-annotatie van regeltekst |
| `activiteit` | dso | IMOW-activiteit, gedefinieerd in omgevingsdocument |
| `activiteit_locatieaanduiding` | dso | Koppeling activiteit-locatie |
| `gebiedsaanwijzing` | dso | IMOW-object |
| `juridische_regel_gebiedsaanwijzing` | dso | Junction |
| `norm` | dso | IMOW-norm |
| `normwaarde` | dso | IMOW-normwaarde |
| `juridische_regel_norm` | dso | Junction |
| `tekstdeel` | dso | IMOW-tekstdeel |
| `hoofdlijn` | dso | IMOW-hoofdlijn |
| `tekstdeel_hoofdlijn` | dso | Junction |
| `pons` | dso | Hoort bij Ow-zijde van Wro-Ow overgang |
| `kaart` | dso | Viewer-metadata gekoppeld aan IMOW-objecten (volgt IMOW-definities) |
| `kaartlaag` | dso | Koppelt kaart aan gebiedsaanwijzing/norm/activiteit |

### `wro` — oud regime (7 tabellen)

Het Wro-/IMRO-stelsel. Apart schema vanwege eigen technische stack, eigen
loaders en sunset in 2032. PONS blijft in `p2p` (staat op de Ow-kant van de
overgang).

| Tabel | Nu in | Reden |
|-------|-------|-------|
| `wro_manifest` | dso | Bronhouder-manifest voor Wro-plannen |
| `wro_dossier` | dso | Wro-dossier |
| `ruimtelijk_instrument` | dso | Bestemmingsplan/inpassingsplan |
| `planobject` | dso | IMRO-planobject |
| `wro_tekst_object` | dso | Regels en toelichting bij bestemmingsplan |
| `wro_geleideformulier` | dso | Metadata bij bestemmingsplan |
| `wro_bronbestand` | dso | Bestandslijst bij bestemmingsplan |

### `i2a` — idee-tot-afhandeling (7 tabellen)

Toepasbare regels (STTR/IMTR), werkzaamheden, aansluitpunten. Later
uitgebreid met vragenbomen.

| Tabel | Nu in | Reden |
|-------|-------|-------|
| `regelbeheerobject` | dso | Brug IMOW-activiteit ↔ STTR-regelbestand, maar thuishoort bij STTR-zijde |
| `toepasbaar_regelbestand` | dso | STTR-bestand — kern van i2a |
| `dmn_element` | dso | Onderdeel van STTR |
| `uitvoeringsregel` | dso | Onderdeel van STTR |
| `werkzaamheid` | dso | SVB-BG catalogus voor vergunningcheck |
| `aansluitpunt` | dso | Digitaal stelsel-registratie |
| `aansluiting` | dso | Koppeling aansluitpunt ↔ activiteit/bronhouder/regelbestand |

### `v2a` — vraag-tot-antwoord (0 tabellen nu)

Nu leeg. Gereserveerd voor:

- `vergunning`, `vergunning_locatie` (idee #8 uit ideeen.md)
- Zoekindex-caches (pgvector embeddings, full-text materialized views)
- Viewer-gerichte aggregaties die niet in p2p/i2a thuishoren

---

## Cross-schema FK's (verwacht, geen probleem)

Postgres ondersteunt cross-schema FK's zonder meerkosten. Verwachte links:

**p2p → core:**
- `regeling.bronhouder → core.bronhouder`
- `regeling.documenttype → core.documenttype`
- `regeling.regelingmodel → core.regelingmodel`
- `besluit.bronhouder → core.bronhouder`
- `besluit.besluitmodel → core.besluitmodel`
- `juridische_regel.idealisatie → core.idealisatie`

**wro → core:**
- `wro_manifest.overheidscode → core.bronhouder`
- `wro_dossier.status → core.dossierstatus`
- `ruimtelijk_instrument.bronhouder → core.bronhouder`
- `ruimtelijk_instrument.planstatus → core.planstatus`
- `planobject.bestemmingshoofdgroep → core.bestemmingshoofdgroep`
- `planobject.bouwaanduidingtype → core.bouwaanduidingtype`
- `planobject.figuurtype → core.figuurtype`
- `planobject.gebiedsaanduidinghoofdgroep → core.gebiedsaanduidinghoofdgroep`

**i2a → p2p:**
- `regelbeheerobject.activiteit_id → p2p.activiteit`
- `werkzaamheid.activiteit_id → p2p.activiteit`
- `aansluiting.activiteit_id → p2p.activiteit`

**i2a → core:**
- `aansluiting.bronhouder → core.bronhouder`

**p2p ↔ wro (geen harde FK):**
- `pons.was_bestemmingsplan` verwijst naar een Wro-plan-IDN maar is in de
  DDL geen harde FK — geen schema-grensprobleem.

De i2a→p2p richting bevestigt dat `regelbeheerobject` en `werkzaamheid` in
i2a horen: ze refereren aan p2p, niet andersom. De wro→core richting laat
zien dat `wro` compleet los staat van `p2p`.

---

## Besliste punten

1. **`regelbeheerobject` → `i2a`** (STTR-kant volgt de FK-richting).
2. **`werkzaamheid` → `i2a`** (cataloguskarakter, maar primair voor vergunningcheck).
3. **`kaart` / `kaartlaag` → `p2p`** (volgt IMOW-definities, FK's naar gebiedsaanwijzing/norm/activiteit).
4. **Wro krijgt eigen schema `wro`** (technische stack los, sunset 2032).
5. **Code qualifieren** in `query.py` en loaders — `search_path` niet als escape gebruiken, dat is juist de winst.
6. **`aansluitpunt` / `aansluiting` → `i2a`** (open voor verplaatsing naar v2a als dat schema concreter wordt).

---

## Impact op bestaande data

**Databasenivo:** minimaal.
- `ALTER TABLE dso.foo SET SCHEMA p2p;` is een metadata-operatie, seconden per tabel.
- FK's, indices, GIST-indices (PostGIS), constraints verhuizen automatisch mee.
- Geen rijherschrijving, geen downtime-risico bij huidige DB-grootte.

**Codenivo:** middelgroot.
- `src/ddl.py`: herstructureren per schema (het leidende document bij migratie).
- `src/query.py`: 18× `FROM`, 16× `JOIN` — worden qualified (`p2p.regeling`,
  `i2a.dmn_element`, `wro.planobject`, `core.bronhouder`). Maakt cross-keten
  queries expliciet leesbaar.
- `src/loaders/*`: qualifiers toevoegen per loader-module.
- `src/parsers/*`: waarschijnlijk geen DB-toegang.
- `ocd-api/main.py` + `db.py`: controleren op hard-coded references.

**Configuratienivo:** klein.
- Read-only rol per schema voor API's (straks).
- `search_path` default voor applicatie-user bijzetten.
- Loaders: default target-schema per loader-module.

---

## Migratievolgorde (bij groen licht)

1. **Overzicht goedkeuren** (dit document).
2. **DDL herstructureren** in `src/ddl.py`: nieuwe `CREATE SCHEMA`-statements,
   tabellen per sectie gegroepeerd per schema, FK's qualified.
3. **Migratiescript** `scripts/migrate_to_keten_schemas.sql`:
   idempotent, één transactie per schema, met rollback-plan.
4. **Codescan** voor hard-coded `dso.`-verwijzingen en unqualified refs.
5. **Regressietest** tegen pre-migratie snapshot (row counts + checksum per tabel).
6. **`query.py` refactor** met qualified references.
7. **Documentatie**: README's van `dso-loader` en `ocd-api` updaten.
8. **Oude `dso`-schema droppen** pas nadat alles groen is.

---

## Resterend besluit

- [ ] Oude `dso`-schema direct droppen na succesvolle migratie, of tijdelijk
      als alias/views laten staan voor externe tooling tijdens transitie?
