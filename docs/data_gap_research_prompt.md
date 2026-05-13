# Data-gap onderzoek prompt — OCD pipeline-gaps voor omgevingsbot

Deze prompt is bedoeld om in een nieuwe Claude Code-sessie (of vergelijkbare
agent) te plakken. Je bent de OCD data-engineering-onderzoeker. Een aparte
chatbot-pipeline (omgevingsbot) heeft 19 cases geïdentificeerd waarbij
specifieke regelingen ontbreken in de pipeline-state voor bepaalde
locaties. Jouw taak: per case bepalen of het een data-acquisitie-,
geo-koppeling- of naam-mismatch-probleem is.

---

## Achtergrond

De omgevingsbot-pipeline draait `/v1/adres` op de OCD-API om voor een
locatie + vraag de relevante regelingen op te halen. Voor 19 testcases mist
de pipeline-state een specifieke verwachte regeling (bv.
"Waterschapsverordening Wetterskip Fryslân 2023", "Omgevingsverordening
NH2022", "Programma Integraal Riviermanagement", "Ontwerp Nota Ruimte").

We weten niet of dit komt door:
- **DATA-GAP**: de regeling is nooit ingegest in OCD
- **GEO-KOPPELING**: de regeling bestaat maar de geo-koppeling matcht niet
- **NAAM-MISMATCH**: de regeling staat onder een net andere naam in DB
- **FILTER-BUG**: regeling + geo zijn er, maar `/v1/adres` filtert 'm weg

## Context

OCD draait op PostgreSQL 16 met PostGIS. Connection via `db.py` in
`C:\GIT\OCD\ocd-api\` — `get_conn()` levert een psycopg-cursor met
`dict_row`. Run vanuit `C:\GIT\OCD\ocd-api\` met `.venv\Scripts\python.exe`.

Schema's:
- `p2p` — Ow-regelgeving (Omgevingsplan, Omgevingsverordening,
  Waterschapsverordening, Programma's, Omgevingsvisies). Tabellen:
  `regeling`, `juridische_regel`, `tekst_element`, `activiteit`, `norm`,
  `gebiedsaanwijzing`, `locatie`, `activiteit_locatieaanduiding`,
  `juridische_regel_norm`, `juridische_regel_gebiedsaanwijzing`.
- `wro` — Wro-regelgeving (Bestemmingsplannen). Tabellen:
  `ruimtelijk_instrument`, `planobject`, `wro_tekst_object`.
- `conv` — Geconverteerde Wro→Ow data.

Belangrijke velden:
- `p2p.regeling.opschrift` (regelingsnaam zoals "Omgevingsverordening NH2022")
- `p2p.regeling.bronhouder` (bronhouder-code: gm0344, pv21, mnre1153, etc.)
- `p2p.regeling.documenttype` (Omgevingsplan, Omgevingsverordening,
  Waterschapsverordening, Programma, Omgevingsvisie, etc.)
- `p2p.locatie.geometrie` (Polygon/MultiPolygon in EPSG:28992)
- `wro.ruimtelijk_instrument.naam` (BP-naam zoals "Bestemmingsplan
  Earnewâld 2013")

## Input-data

Lees deze twee JSON-bestanden:

1. `C:\GIT\omgevingsbot.nl\backend\tests\evaluation\test_cases.json` — alle
   40 testcases met `id`, `location` (adres of "Perceel X" of "RD x,y"),
   `question`, `bron_naam` (verwachte regeling), `expected_source_contains`,
   `bevoegd_gezag`, `regime` (OW/RO).
2. `C:\GIT\omgevingsbot.nl\backend\tests\evaluation\last_pipeline_results.json`
   — L1-eval-output. Voor elke PARTIAL-case staan de gefaalde checks;
   meestal `<state>: does not contain "<regeling-naam>"`.

## De te onderzoeken cases

Lees `last_pipeline_results.json`, filter op `status="PARTIAL"`. Voor elke
case heb je:
- `case_id`
- `location` (uit test_cases.json — moet je bij elkaar joinen op id)
- `bron_naam` (de regeling die we verwachten)
- `state_summary.location.rd_x/rd_y` (de geo-coordinaten waar /v1/adres
  naar zoekt)

Als je de RD-coords niet hebt voor een case, kan je ze ophalen door:

```bash
curl 'http://127.0.0.1:8080/v1/adres?q=<urlencoded_locatie>' | jq '.rd'
```

## Systematisch diagnose-protocol

Per case voer je drie checks uit.

### Check 1: Bestaat de regeling überhaupt in de DB?

```sql
SELECT frbr_expression, opschrift, documenttype, bronhouder
FROM p2p.regeling
WHERE opschrift ILIKE '%<bron_naam_substring>%'
   OR opschrift ILIKE '%<naam_zonder_jaar>%'
LIMIT 10;
```

Plus voor Wro:

```sql
SELECT idn, naam, type_plan, datum
FROM wro.ruimtelijk_instrument
WHERE naam ILIKE '%<bron_naam_substring>%';
```

**Verdict-uitkomsten**:
- 0 rijen → **DATA-GAP**: regeling is niet ingegest. Vereist loader-uitbreiding.
- 1+ rijen met andere naam → **NAAM-MISMATCH**: regeling bestaat onder iets
  andere naam in de DB. Pipeline-fix of test-update.
- 1+ rijen met exacte naam → ga door naar Check 2.

### Check 2: Heeft deze regeling locaties die geometric matchen op (rd_x, rd_y)?

```sql
WITH reg AS (
    SELECT frbr_expression FROM p2p.regeling
    WHERE opschrift = '<exact_naam>'
)
SELECT COUNT(DISTINCT l.identificatie) AS n_locs,
       MIN(ST_Distance(l.geometrie,
           ST_SetSRID(ST_MakePoint(<rd_x>, <rd_y>), 28992))) AS min_dist_m
FROM p2p.locatie l
JOIN p2p.tekst_element te ON te.regeling_expression IN (SELECT * FROM reg)
JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
LEFT JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
LEFT JOIN p2p.juridische_regel_norm jrn ON jrn.juridische_regel_id = jr.identificatie
LEFT JOIN p2p.norm n ON n.identificatie = jrn.norm_id
LEFT JOIN p2p.juridische_regel_gebiedsaanwijzing jrg ON jrg.juridische_regel_id = jr.identificatie
LEFT JOIN p2p.gebiedsaanwijzing ga ON ga.identificatie = jrg.gebiedsaanwijzing_id
WHERE l.identificatie IN (ala.locatie_id, n.identificatie, ga.locatie_id);
```

Plus check op locaties die de query-coords intersecten:

```sql
SELECT l.identificatie, l.locatie_type
FROM p2p.locatie l
WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint(<rd_x>, <rd_y>), 28992))
LIMIT 5;
```

**Verdict-uitkomsten**:
- `n_locs=0` → **GEEN GEO-KOPPELING**: regeling heeft locaties maar niet bij
  deze coördinaten. Onderzoek of de coords binnen het regelingsgebied vallen.
- `min_dist_m > 0` (locaties bestaan maar intersecten niet) → **GEO-MISMATCH**:
  PDOK-resolved coords vallen net buiten het regelingsgebied (precisieprobleem
  of bronlocatie-onnauwkeurigheid).
- `n_locs > 0 met min_dist_m=0` → ga door naar Check 3.

### Check 3: Waarom valt deze regeling buiten /v1/adres-output?

Roep direct het endpoint aan:

```bash
curl 'http://127.0.0.1:8080/v1/adres?q=<urlencoded_locatie>' | jq '.ow_regels[] | .regeling' | sort -u
```

Vergelijk met de query-output uit Check 2. Als de regeling-naam in de DB
bestaat én locatie-koppeling klopt, maar niet in `/v1/adres`-output
verschijnt, dan is er een **filter-bug in `_wat_geldt_hier()`** in
`C:\GIT\OCD\ocd-api\main.py`:
- Mogelijk: `documenttype` filter sluit dit type uit.
- Mogelijk: zoektermen-filter is te aggressief (zoals de wro-bug die al
  gefixt is).

## Output-format

Per case een rij in een Markdown-tabel:

| case_id | bron_naam | check_1 | check_2 | check_3 | verdict | aanbevolen_fix |
|---|---|---|---|---|---|---|
| r21-... | Waterschapsverordening Wetterskip Fryslân 2023 | 1 rij gevonden | n_locs=0 | n.v.t. | GEEN GEO-KOPPELING | Investigate locatie-koppeling in dso-loader |
| r34-... | Programma Integraal Riviermanagement | 0 rijen | n.v.t. | n.v.t. | DATA-GAP | Loader uitbreiden |
| r25-... | Omgevingsverordening NH2022 | 0 rijen exact, 1 als "Omgevingsverordening Noord-Holland 2022" | n_locs=12 | regelingen-list bevat | NAAM-MISMATCH | test_cases.json bron_naam corrigeren |

Aan het eind een **groep-samenvatting**:
- DATA-GAP totaal: N cases (welke loader-uitbreiding(en) nodig)
- GEO-KOPPELING totaal: N cases (welke geo-fix)
- NAAM-MISMATCH totaal: N cases (welke test-correcties)
- FILTER-BUG totaal: N cases (welke endpoint-aanpassingen)

## Werkwijze

1. Begin met cases waar de regeling-naam **uniek** is — die zijn snel te
   diagnose-en (bv. "Programma Integraal Riviermanagement" — bestaat het of
   niet?).
2. Doe vergelijkbare cases samen (alle Omgevingsverordeningen, alle
   Waterschapsverordeningen).
3. Stop tussentijds met een korte samenvatting per cluster zodat de
   gebruiker kan meekijken voordat je de volgende cluster aanpakt.

## Bekende beperkingen

- Een eerdere bug in `/v1/adres` (keyword-filter op `wro_bestemmingen`) is
  recent gefixt. De L1-eval is na deze fix gedraaid, dus oude wro-issues
  zouden uit moeten zijn.
- De `juridische_regel.regeltekst_wid` → `regeling_expression`-koppeling
  is **niet uniek**: dezelfde wid kan in 100+ regelingen voorkomen. Voor
  Aanwijzingsbesluit Natura 2000-cases is dit een bekende complicatie. Deze
  case mag je apart markeren als "AMBIGUE-LINK".

Begin nu met cluster 1 (rijksbeleid: Programma Integraal Riviermanagement,
Ontwerp Nota Ruimte). Geef per case een verdict-rij in de tabel en sluit
de cluster af met een mini-samenvatting voordat je de volgende start.
