# OCD API

FastAPI wrapper over de lokale OCD PostGIS database (`dso` op `localhost:5434`).

## Lokaal draaien

```bash
cd ocd-api
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
# edit .env — OCD_API_KEY mag leeg blijven voor lokaal

uvicorn main:app --reload --port 8080
```

Open:
- http://localhost:8080/docs — Swagger UI
- http://localhost:8080/health
- http://localhost:8080/v1/overzicht
- http://localhost:8080/v1/adres?q=Prinsengracht+263+Amsterdam

Als `OCD_API_KEY` gezet is, stuur `X-Api-Key: <key>` mee.

## Endpoints

| Pad | Functie |
|---|---|
| `GET /health` | DB-check |
| `GET /v1/adres?q=...` | Adres → RD → Ow-regels + Wro-bestemmingen |
| `GET /v1/locatie?x=...&y=...` | RD-coordinaten → regels + bestemmingen |
| `GET /v1/zoek?q=...` | Full-text ILIKE over Ow + Wro teksten |
| `GET /v1/gezagen` | Bronhouders met laad-status |
| `GET /v1/overzicht` | Totalen per tabel |
| `GET /v1/vergunningen` | Gefilterde lijst van omgevingsvergunning-kennisgevingen |
| `GET /v1/vergunningen/pins` | Lichtgewicht pin-only voor kaartweergave (gecapt) |
| `GET /v1/vergunningen/facets` | Filter-counters voor huidige filterset |
| `GET /v1/vergunningen/stats` | Totalen voor header (totaal, laatste publicatie/ingest) |
| `GET /v1/vergunningen/{koop_id}` | Volledige record-details + GeoJSON-geometrie |

### Vergunningen-API (backend voor omgevingsvergunningenregister.nl)

Leest uit `vth.vergunningkennisgeving`. Gedeelde query-parameters over
list / pins / facets:

| Param | Type | Voorbeeld |
|---|---|---|
| `q` | string | `?q=dakopbouw` |
| `tb` | repeatable | `?tb=aanvraag&tb=verleend` |
| `ac` | repeatable | `?ac=bouwen` |
| `bg` | repeatable | `?bg=Amsterdam&bg=Utrecht` |
| `org` | repeatable | `?org=gemeente&org=provincie` |
| `th` | repeatable | `?th=Ruimte%20en%20infrastructuur%20\|%20Organisatie%20en%20beleid` |
| `vanaf` | date | `?vanaf=2026-01-01` |
| `totd` | date | `?totd=2026-05-19` |
| `geom` | bool | `?geom=true` (alleen met geometrie) |
| `ontv` | bool | `?ontv=true` (alleen met datum_ontvangst) |
| `zaak` | string | `?zaak=Z-2025` (ILIKE) |
| `bbox` | `W,S,E,N` WGS84 | `?bbox=4.85,52.35,4.95,52.40` |

List heeft daarnaast `sort=datum|datum_asc|ontvangst|bg`, `limit` (≤500),
`offset`. Pins-endpoint heeft `cap` (≤50.000, default 10.000) en
forceert `geom=true`.

Quick test:
```bash
curl http://localhost:8001/v1/vergunningen/stats
curl 'http://localhost:8001/v1/vergunningen?bg=Alkmaar&tb=verleend&limit=2'
curl 'http://localhost:8001/v1/vergunningen/pins?bbox=4.85,52.35,4.95,52.40'
curl http://localhost:8001/v1/vergunningen/gmb-2026-234803
```

Zie [../dso-loader/DEPLOY.md](../dso-loader/DEPLOY.md) voor Railway-deploy.
