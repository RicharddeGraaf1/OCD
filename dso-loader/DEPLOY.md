# OCD API — Railway Deployment Plan

## Overzicht

De OCD-database (9,4M rows, ~5 GB) beschikbaar maken als REST API via Railway.
Drie services: PostGIS database, FastAPI service, optioneel cron voor data-refresh.

```
Railway Project
├── PostGIS service     (~5 GB, PostgreSQL 17 + PostGIS 3.5)
├── FastAPI service     (OCD API, publiek bereikbaar)
└── (optioneel) Cron    (wekelijkse data-refresh)
```

## Schema-indeling

De database is keten-gedreven opgesplitst (zie `src/ddl.py`):

| Schema | Inhoud |
|---|---|
| `core` | Waardelijsten + `bronhouder` |
| `p2p`  | Plan-tot-publicatie (Ow): regelingen, besluiten, CIM-OW objecten (locatie, activiteit, juridische_regel, norm, tekstdeel, pons, kaart, ...) |
| `wro`  | Oud regime: Wro/IMRO bestemmingsplannen (sunset 2032) |
| `i2a`  | Idee-tot-afhandeling: IMTR toepasbare regels, werkzaamheden, aansluitpunt |
| `v2a`  | Vraag-tot-antwoord (gereserveerd, nu leeg) |

Alle SQL in deze doc gebruikt deze schemas expliciet — het oude `dso.*` schema bestaat niet meer.

---

## Stap 1: PostGIS database aanmaken

1. Ga naar https://railway.com/deploy/postgis-17 (PG 17 + PostGIS 3.5, 1-click deploy)
2. Railway maakt automatisch een database aan met connection details
3. Noteer de `DATABASE_URL` uit **Project Dashboard → Variables**

## Stap 2: Lokale database exporteren en uploaden

```bash
cd C:\GIT\OCD\dso-loader
.venv\Scripts\activate

# Export lokale database (poort 5434)
pg_dump -h localhost -p 5434 -U postgres -d dso -F c -Z 5 -f ocd_backup.dump

# Restore naar Railway (vervang met echte Railway connection details)
pg_restore \
  -h <railway-host>.railway.internal \
  -p <railway-port> \
  -U postgres \
  -d railway \
  -F c \
  --no-owner \
  --no-privileges \
  ocd_backup.dump
```

**Let op:**
- De dump is ~1-2 GB gecomprimeerd (5 GB uncompressed)
- Railway vereist **Public Networking** (TCP proxy) op de database voor de restore
- Na de restore kun je Public Networking weer uitzetten
- PostGIS extensie is al geinstalleerd via het template

## Stap 3: FastAPI service opzetten

### Projectstructuur

```
ocd-api/
├── main.py              # FastAPI endpoints
├── db.py                # Database connection pool
├── requirements.txt     # Dependencies
├── Procfile             # Railway start command
└── railway.toml         # Railway config
```

### requirements.txt

```
fastapi>=0.115
uvicorn[standard]>=0.30
psycopg[binary]>=3.2
python-dotenv>=1.0
```

### Procfile

```
web: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
```

### railway.toml

```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"
healthcheckPath = "/health"
healthcheckTimeout = 5
```

### db.py

```python
import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
pool = ConnectionPool(DATABASE_URL, kwargs={"row_factory": dict_row}, min_size=2, max_size=10)

@contextmanager
def get_conn():
    with pool.connection() as conn:
        yield conn
```

### main.py — Endpoints

```python
from fastapi import FastAPI, Query, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
import os
import httpx
from db import get_conn

app = FastAPI(
    title="OCD API",
    description="Omgevingswet Centraal Datamodel — alle regelgeving van Nederland",
    version="1.0",
)

# --- Auth ---
API_KEY = os.environ.get("OCD_API_KEY", "")
api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

async def verify_key(key: str = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

# --- Health ---
@app.get("/health")
def health():
    return {"status": "ok"}

# --- Endpoints ---
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

@app.get("/v1/adres", dependencies=[Depends(verify_key)])
def adres(q: str = Query(..., description="Adres (bijv. 'Prinsengracht 263, Amsterdam')")):
    """Wat geldt op een adres? Cross-regime: Ow-regels + Wro-bestemmingen."""
    # Resolve adres naar RD
    resp = httpx.get(LOCATIESERVER, params={"q": q, "rows": 1, "fq": "type:adres"}, timeout=10)
    docs = resp.json().get("response", {}).get("docs", [])
    if not docs:
        raise HTTPException(404, "Adres niet gevonden")
    doc = docs[0]
    coords = doc["centroide_rd"].replace("POINT(", "").replace(")", "").split()
    x, y = float(coords[0]), float(coords[1])
    label = doc.get("weergavenaam", q)
    return {"adres": label, "rd": {"x": x, "y": y}, **_wat_geldt_hier(x, y)}

@app.get("/v1/locatie", dependencies=[Depends(verify_key)])
def locatie(x: float = Query(...), y: float = Query(...)):
    """Wat geldt op RD-coordinaten?"""
    return _wat_geldt_hier(x, y)

def _wat_geldt_hier(x: float, y: float):
    with get_conn() as conn:
        cur = conn.cursor()
        # Ow-regels
        cur.execute("""
            SELECT r.opschrift, r.documenttype, te.opschrift as artikel, te.inhoud,
                   a.naam as activiteit, jr.regel_type
            FROM p2p.activiteit_locatieaanduiding ala
            JOIN p2p.locatie l ON l.identificatie = ala.locatie_id
            JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
            JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            JOIN p2p.activiteit a ON a.identificatie = ala.activiteit_id
            WHERE ST_Contains(l.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            LIMIT 50
        """, (x, y))
        ow = cur.fetchall()
        # Wro-bestemmingen
        cur.execute("""
            SELECT ri.naam as plan, po.object_type, po.naam as bestemming,
                   po.bestemmingshoofdgroep
            FROM wro.planobject po
            JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
            WHERE ST_Contains(po.geometrie, ST_SetSRID(ST_MakePoint(%s, %s), 28992))
            LIMIT 50
        """, (x, y))
        wro = cur.fetchall()
    return {"ow_regels": ow, "wro_bestemmingen": wro}

@app.get("/v1/zoek", dependencies=[Depends(verify_key)])
def zoek(q: str = Query(..., min_length=2), limit: int = Query(20, le=100)):
    """Full-text search over 2M+ teksten (Ow + Wro cross-regime)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            (SELECT 'Ow' as regime, r.opschrift as document, te.opschrift as artikel,
                    LEFT(te.inhoud, 500) as tekst
             FROM p2p.tekst_element te
             JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
             WHERE te.inhoud ILIKE %s
             LIMIT %s)
            UNION ALL
            (SELECT 'Wro', ri.naam, wt.naam,
                    LEFT(wt.inhoud, 500)
             FROM wro.wro_tekst_object wt
             JOIN wro.ruimtelijk_instrument ri ON ri.idn = wt.instrument_idn
             WHERE wt.inhoud ILIKE %s
             LIMIT %s)
        """, (f"%{q}%", limit, f"%{q}%", limit))
        return {"resultaten": cur.fetchall(), "zoekterm": q}

@app.get("/v1/gemeente/{code}/activiteiten", dependencies=[Depends(verify_key)])
def activiteiten(code: str):
    """Alle activiteiten van een gemeente."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT a.naam, a.groep, ala.kwalificatie
            FROM p2p.activiteit a
            JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
            WHERE a.identificatie LIKE %s
            ORDER BY a.naam
        """, (f"%gm{code}%",))
        return {"gemeente": code, "activiteiten": cur.fetchall()}

@app.get("/v1/gemeente/{code}/normen", dependencies=[Depends(verify_key)])
def normen(code: str):
    """Alle omgevingsnormen van een gemeente."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.naam, n.type_norm, n.eenheid, n.groep,
                   count(nw.id) as aantal_waarden
            FROM p2p.norm n
            JOIN p2p.normwaarde nw ON nw.norm_id = n.identificatie
            WHERE n.identificatie LIKE %s
            GROUP BY n.identificatie
            ORDER BY n.naam
        """, (f"%gm{code}%",))
        return {"gemeente": code, "normen": cur.fetchall()}

@app.get("/v1/gemeente/{code}/pons", dependencies=[Depends(verify_key)])
def pons(code: str):
    """Pons-status: hoeveel Wro-plannen, is er een pons?"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) as wro_instrumenten
            FROM wro.ruimtelijk_instrument
            WHERE bronhouder = %s
        """, (code,))
        wro_count = cur.fetchone()["wro_instrumenten"]
        cur.execute("""
            SELECT count(*) as pons_count
            FROM p2p.pons p
            WHERE p.identificatie LIKE %s
        """, (f"%gm{code}%",))
        pons_count = cur.fetchone()["pons_count"]
        return {"gemeente": code, "wro_instrumenten": wro_count, "pons_aanwezig": pons_count > 0}

@app.get("/v1/gezagen", dependencies=[Depends(verify_key)])
def gezagen():
    """Alle bevoegde gezagen met laad-status."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT overheidscode, naam, bestuurslaag,
                   ow_geladen, imtr_geladen, wro_geladen,
                   ow_regelingen, wro_instrumenten
            FROM core.bronhouder
            ORDER BY naam
        """)
        return {"bronhouders": cur.fetchall()}

@app.get("/v1/overzicht", dependencies=[Depends(verify_key)])
def overzicht():
    """Database-overzicht: totalen per tabel."""
    tables = [
        ("core", "bronhouder"),
        ("p2p", "regeling"), ("p2p", "tekst_element"), ("p2p", "juridische_regel"),
        ("p2p", "activiteit"), ("p2p", "locatie"), ("p2p", "gebiedsaanwijzing"),
        ("p2p", "norm"), ("p2p", "normwaarde"),
        ("i2a", "toepasbaar_regelbestand"), ("i2a", "dmn_element"), ("i2a", "werkzaamheid"),
        ("wro", "ruimtelijk_instrument"), ("wro", "planobject"), ("wro", "wro_tekst_object"),
    ]
    with get_conn() as conn:
        cur = conn.cursor()
        counts = {}
        for schema, t in tables:
            cur.execute(f"SELECT count(*) as n FROM {schema}.{t}")
            counts[t] = cur.fetchone()["n"]
    return {"tabellen": counts, "totaal": sum(counts.values())}
```

## Stap 4: Deployen naar Railway

```bash
cd ocd-api

# Railway CLI installeren (als je dat nog niet hebt)
npm install -g @railway/cli

# Inloggen
railway login

# Project aanmaken
railway init

# Environment variables instellen
railway variables set OCD_API_KEY="jouw-gekozen-api-key"
# DATABASE_URL wordt automatisch gekoppeld als je de PostGIS service linkt

# Deployen
railway up
```

Railway geeft je een URL: `https://ocd-api-production.up.railway.app`

## Stap 5: Testen

```bash
# Health check
curl https://ocd-api-production.up.railway.app/health

# Adres opvragen
curl -H "X-Api-Key: jouw-key" \
  "https://ocd-api-production.up.railway.app/v1/adres?q=Prinsengracht+263+Amsterdam"

# Full-text zoeken
curl -H "X-Api-Key: jouw-key" \
  "https://ocd-api-production.up.railway.app/v1/zoek?q=bouwhoogte"

# Swagger docs
open https://ocd-api-production.up.railway.app/docs
```

## Stap 6: Omgevingsbot aansluiten

In `C:\GIT\Omgevingsbot.nl\backend\services\dso_service.py` de DSO API-calls aanvullen/vervangen:

```python
OCD_API = "https://ocd-api-production.up.railway.app"
OCD_KEY = os.environ["OCD_API_KEY"]

async def search_via_ocd(adres: str):
    """1 call in plaats van 4-6 DSO API calls."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{OCD_API}/v1/adres",
            params={"q": adres},
            headers={"X-Api-Key": OCD_KEY}
        ) as resp:
            return await resp.json()
```

---

## Beveiliging

| Maatregel | Hoe |
|-----------|-----|
| API key authenticatie | `X-Api-Key` header op alle endpoints |
| Database niet publiek | Private Networking tussen FastAPI en PostGIS |
| Read-only DB user | `CREATE USER ocd_reader WITH PASSWORD '...'; GRANT USAGE ON SCHEMA core, p2p, wro, i2a TO ocd_reader; GRANT SELECT ON ALL TABLES IN SCHEMA core, p2p, wro, i2a TO ocd_reader;` |
| Rate limiting | FastAPI middleware of Railway's ingebouwde rate limiting |
| CORS | Beperken tot bekende origins (omgevingsbot.nl, localhost) |

## Kosten (Railway Pro)

| Component | Geschat |
|-----------|---------|
| PostGIS (5 GB data, 2 GB RAM) | ~$10/maand |
| FastAPI (256 MB RAM) | ~$5/maand |
| Bandbreedte | Inclusief bij Pro |
| **Totaal** | **~$15/maand** |

## Data-versheid

De OCD-database is een snapshot. Opties om actueel te blijven:

1. **Handmatig**: herlaad specifieke gemeenten via de CLI wanneer nodig
2. **Wekelijks cron** (Railway cron service): herlaad alle bronhouders incrementeel
3. **Op verzoek**: een `/admin/refresh/{gemeente}` endpoint dat de loader triggert

Voor de meeste use cases is een maandelijkse refresh voldoende — regelgeving verandert niet dagelijks.
