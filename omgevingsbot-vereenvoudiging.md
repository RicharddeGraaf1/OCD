# Omgevingsbot.nl — Vereenvoudiging via lokale DSO-database

Plan voor het vervangen van live DSO/IHR API-calls in omgevingsbot.nl
door queries op de lokale Postgres+PostGIS database (`dso` op
`localhost:5434`).

---

## Huidige situatie

De omgevingsbot doet **per gebruikersvraag 5-7 live API-calls** naar
externe services, elk met latency en rate-limits:

```
Gebruiker → "Mag ik een dakkapel in Utrecht?"
  1. PDOK locatieserver         → RD-coördinaten         (~100ms)
  2. DSO Presenteren /_zoek     → regelingen op punt      (~200ms)
  3. DSO Presenteren /onderwerpen → activiteiten op punt  (~300ms)
  4. DSO Presenteren /annotaties → artikeltekst           (~500ms)
  5. DSO Ontsluiten /_suggereer → zoekresultaten          (~200ms)
  6. IHR /plannen               → Wro-fallback            (~200ms)
  7. IHR /plannen/{id}/teksten  → plankekst               (~200ms)
  8. LLM                        → antwoord genereren
                                                    Totaal: ~1.7 sec
```

### Bronbestanden in omgevingsbot.nl

| Bestand | Regels | Functie |
|---|---|---|
| `backend/services/dso_service.py` | 1172 | Alle DSO/PDOK/IHR API-integratie |
| `backend/services/chat_service.py` | 759 | Orchestratie en pipeline-logica |
| `backend/config.py` | ~50 | API-URLs en keys |

### Externe API's die aangeroepen worden

| API | Base URL | Auth | Wat het doet |
|---|---|---|---|
| DSO Ontsluiten v2 | `service.omgevingswet.overheid.nl/publiek/omgevingsinformatie/api/ontsluiten/v2` | API-key | Fuzzy document-zoek |
| DSO Presenteren v8 | `service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8` | API-key | Regelingen, annotaties, onderwerpen op locatie |
| PDOK Locatieserver | `api.pdok.nl/bzk/locatieserver/search/v3_1/free` | Geen | Adres → RD-coördinaten |
| IHR (Ruimtelijke Plannen) | `ruimte.omgevingswet.overheid.nl/ruimtelijke-plannen/api/opvragen/v4` | API-key | Wro-bestemmingsplannen fallback |

---

## Nieuwe situatie met lokale database

```
Gebruiker → "Mag ik een dakkapel in Utrecht?"
  1. PDOK locatieserver         → RD-coördinaten         (~100ms)
  2. DB: p2p + wro puntquery    → cross-regime alles      (~20ms)
  3. DB: tekst + activiteiten   → artikeltekst + context   (~30ms)
  4. LLM                        → antwoord genereren
                                                    Totaal: ~200ms
```

**Van 5-7 API-calls naar 1 API-call + 2-3 DB-queries.**

---

## Concrete vervangingen per API-call

### 1. `POST /regelingen/_zoek` → DB query

**Huidige code** (`dso_service.py:306`):
```python
response = await self._presenteren_request("POST", "/regelingen/_zoek", json={
    "_geo": {"_geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
})
```

**Vervanging**:
```sql
SELECT DISTINCT r.frbr_expression, r.opschrift, r.citeertitel, r.regelingmodel
FROM p2p.regeling r
JOIN p2p.tekst_element te ON te.regeling_expression = r.frbr_expression
JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint($1, $2), 28992))
```

**Winst**: ~200ms → ~20ms, geen rate-limit.

### 2. `POST /onderwerpen/_zoek` → DB query

**Huidige code** (`dso_service.py:409`):
```python
response = await self._presenteren_request("POST", "/onderwerpen/_zoek", json={
    "_geo": {"_geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
})
```

**Vervanging**:
```sql
-- Activiteiten op een punt
SELECT DISTINCT a.naam, a.groep, ala.kwalificatie
FROM p2p.activiteit_locatieaanduiding ala
JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint($1, $2), 28992));

-- Gebiedsaanwijzingen op een punt
SELECT ga.naam, ga.type
FROM p2p.gebiedsaanwijzing ga
JOIN p2p.locatie l ON l.identificatie = ga.locatie_id
WHERE ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint($1, $2), 28992));
```

**Winst**: activiteiten + gebiedsaanwijzingen in één query ipv aparte API-call.

### 3. `POST /regeltekstannotaties/_zoek` → DB query

**Huidige code** (`dso_service.py:354`):
```python
response = await self._presenteren_request(
    "POST", f"/regelingen/{uri}/regeltekstannotaties/_zoek", json={...}
)
```

**Vervanging**:
```sql
SELECT te.element_type, te.nummer, te.opschrift, te.inhoud
FROM p2p.tekst_element te
JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
JOIN p2p.activiteit_locatieaanduiding ala ON ala.juridische_regel_id = jr.identificatie
JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
WHERE te.regeling_expression = $1
AND ST_Intersects(l.geometrie, ST_SetSRID(ST_MakePoint($2, $3), 28992))
```

**Winst**: direct de tekst die op een locatie geldt, zonder apart annotaties
te fetchen en dan de tekst erbij te zoeken.

### 4. IHR bestemmingsplannen fallback → DB query

**Huidige code** (`dso_service.py:679-809`):
```python
# Aparte API naar IHR + PONS-check + fallback-logica
response = await self._ihr_request("GET", "/plannen", params={
    "beleidsmatigVerantwoordelijkeOverheid.code": gemeente_code
})
```

**Vervanging**:
```sql
SELECT ri.naam, ri.type_plan, po.object_type, po.naam as bestemming
FROM wro.ruimtelijk_instrument ri
JOIN wro.planobject po ON po.instrument_idn = ri.idn
WHERE ST_Intersects(po.geometrie, ST_SetSRID(ST_MakePoint($1, $2), 28992))
AND ri.pons_status = 'actief'
```

**Winst**: geen aparte IHR-API meer, geen PONS-check nodig (de
`pons_status` kolom vertelt direct of het plan nog geldig is), geen
fallback-logica. Cross-regime is standaard.

### 5. `GET /documenten/_suggereer` → Postgres full-text search

**Huidige code** (`dso_service.py:237`):
```python
response = await self._ontsluiten_request("GET", "/documenten/_suggereer", params={
    "zoekTekst": query, "gemeente.code": gemeente_code
})
```

**Vervanging** (vereist eenmalig een tsvector-kolom + GIN-index):
```sql
-- Full-text search op tekst_element
ALTER TABLE p2p.tekst_element ADD COLUMN IF NOT EXISTS tsv tsvector;
UPDATE p2p.tekst_element SET tsv = to_tsvector('dutch',
    coalesce(opschrift,'') || ' ' || coalesce(inhoud,''));
CREATE INDEX IF NOT EXISTS idx_tekst_fts ON p2p.tekst_element USING GIN(tsv);

-- Query
SELECT te.opschrift, te.inhoud, r.opschrift as regeling
FROM p2p.tekst_element te
JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
WHERE te.tsv @@ plainto_tsquery('dutch', $1)
LIMIT 20
```

**Winst**: geen externe API nodig, werkt offline, sneller.

### 6. PDOK locatieserver → behouden

De PDOK locatieserver is snel (~100ms), betrouwbaar, en open. Het
alternatief (BAG-adressen lokaal laden) is veel werk voor weinig
winst. Aanbeveling: **behouden als enige externe API-call**.

Toekomstige optie: BAG-dataset van PDOK downloaden en lokaal laden,
dan is de bot volledig offline-capable (behalve LLM bij Groq-gebruik).

---

## Implementatie-aanpak

### Stap 1: `db_service.py` toevoegen

Een nieuwe service naast de bestaande `dso_service.py` met dezelfde
interface maar backed door Postgres:

```
backend/services/
├── dso_service.py      # bestaand — live API calls (behouden als fallback)
├── db_service.py       # NIEUW — lokale database queries
├── chat_service.py     # aanpassen: keuze dso_service of db_service
└── ...
```

### Stap 2: Feature flag in config.py

```python
# config.py
USE_LOCAL_DB = os.getenv("USE_LOCAL_DB", "true").lower() == "true"
DB_URL = os.getenv("DSO_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
```

### Stap 3: chat_service.py aanpassen

```python
# chat_service.py
if config.USE_LOCAL_DB:
    from services.db_service import DBService as DataService
else:
    from services.dso_service import DSOService as DataService
```

### Stap 4: Per functie migreren

| Functie in dso_service.py | Vervanging in db_service.py | Prioriteit |
|---|---|---|
| `search_regelingen_at_location()` | `SELECT FROM regeling + locatie` | Hoog |
| `search_onderwerpen_at_location()` | `SELECT FROM activiteit + gebiedsaanwijzing` | Hoog |
| `get_regeltekst_annotaties()` | `SELECT FROM tekst_element + juridische_regel` | Hoog |
| `search_ihr_plannen()` | `SELECT FROM ruimtelijk_instrument + planobject` | Hoog |
| `suggest_documents()` | `Full-text search op tekst_element` | Medium |
| `get_document_structure()` | `WITH RECURSIVE op tekst_element` | Medium |
| `resolve_location()` | Behouden (PDOK) | Laag |

### Stap 5: Keyword-matching verrijken

De huidige `keyword_utils.py` doet synonym-expansie voor DSO-API
zoektermen. Met de lokale database kun je dit verbeteren:

- **Activiteit-matching**: de 100 Utrechtse activiteiten staan al in de
  DB. Match de gebruikersvraag tegen `activiteit.naam` met fuzzy search
  (trigram similarity) ipv keyword-heuristiek.
- **Tekst-relevantie**: Postgres `ts_rank()` geeft een relevantie-score
  die beter is dan de huidige keyword-telling in `chat_service.py`.

---

## Overzicht: wat verandert er

| Aspect | Nu | Straks |
|---|---|---|
| **Latency data-ophaal** | ~1.7 sec (5-7 API calls) | ~200ms (1 API + 2-3 DB queries) |
| **Rate limits** | 10/sec DSO, 14/sec RTR | Unlimited (lokale DB) |
| **Offline-capable** | Nee | Ja (behalve PDOK + LLM) |
| **Betrouwbaarheid** | Afhankelijk van DSO-uptime | Lokale DB altijd beschikbaar |
| **Cross-regime** | Aparte IHR-fallback-logica | Standaard in elke query |
| **Code-complexiteit** | dso_service.py: 1172 regels | db_service.py: ~200 regels |
| **Data-actualiteit** | Realtime | Snapshot (refresh nodig) |

### Trade-off: actualiteit

De enige nadeel van de lokale DB is dat de data een **snapshot** is.
Nieuwe besluiten, gewijzigde regelingen, nieuwe bestemmingsplannen
verschijnen pas na een refresh. Voor de meeste use cases van de
omgevingsbot (informatief, niet juridisch bindend) is dit acceptabel.

Refresh-strategie:
- **Wro**: PDOK update 2×/jaar → `python -m src.cli load-wro`
- **Ow**: delta-update via RTR `_wijzigingen` endpoint → `python -m src.cli delta`
- **IMTR**: regelmatige refresh via STTR API → `python -m src.cli load-imtr`

---

## Database-verbinding

```
Host: localhost
Port: 5434
Database: dso
User: postgres
Password: postgres
Schemas: core, p2p, wro, i2a, v2a

Docker container: dso-postgis (postgis/postgis:16-3.5)
Restart policy: unless-stopped
Backup: C:\GIT\OCD\dso-loader\data\backup\
```

### Nog te bouwen helpers (nice-to-have)

Deze functies/views bestaan nog niet in de DB; als de cross-regime
queries vaker hergebruikt worden is het de moeite waard ze als
DB-objecten te definiëren:

```sql
-- Cross-regime puntquery: alles wat op een punt geldt (Ow + Wro samen)
CREATE OR REPLACE FUNCTION f_alles_op_punt(x double precision, y double precision) ...

-- Unified geometrie-view (Ow-locaties + Wro-planobjecten)
CREATE OR REPLACE VIEW v_geometrie AS ...

-- Tekst-boom navigatie (recursieve subtree van p2p.tekst_element)
CREATE OR REPLACE FUNCTION f_tekst_subtree(parent_id bigint) ...
```

---

## PoC-data beschikbaar (gemeente Utrecht)

| Tabel | Rijen | Inhoud |
|---|---|---|
| p2p.regeling | 1 | Omgevingsplan gemeente Utrecht |
| p2p.tekst_element | 1.745 | Artikelstructuur (Hoofdstuk → Artikel → Lid) |
| p2p.locatie | 3.461 | Gebieden met PostGIS-geometrie |
| p2p.juridische_regel | 763 | Regels voor iedereen |
| p2p.activiteit | 100 | Activiteiten (bouwen, milieu, horeca, etc.) |
| p2p.activiteit_locatieaanduiding | 815 | Koppeling activiteit × locatie × kwalificatie |
| p2p.gebiedsaanwijzing | 7 | Gebiedsaanwijzingen |
| i2a.regelbeheerobject | 169 | IMTR koppelpunten |
| i2a.toepasbaar_regelbestand | 169 | DMN beslisbomen |
| i2a.dmn_element | 4.389 | Decisions + InputData |
| i2a.uitvoeringsregel | 1.724 | Vragen + regels |
| wro.ruimtelijk_instrument | 303 | Wro-bestemmingsplannen |
| wro.planobject | 46.539 | Alle 8 featuretypes met geometrie |
| **Totaal** | **60.186** | |
