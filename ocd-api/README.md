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

Zie [../dso-loader/DEPLOY.md](../dso-loader/DEPLOY.md) voor Railway-deploy.
