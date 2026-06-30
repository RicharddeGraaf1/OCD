"""PoC RP-planvoorraad: bevestig de veldnamen van het RP-Opvragen /plannen endpoint.

Doel: vóór we het `rp.*`-snapshot-schema vastzetten, hard bevestigen welke velden
een plan-object draagt (planstatus, dossierstatus, dossier, identificatie, datums,
en of er iets leverancier-achtigs in zit). Read-only, haalt een handvol plannen op.

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate
    PYTHONPATH=. python scripts/poc_rp_planvoorraad.py [GEMEENTECODE]

Voorbeeld: PYTHONPATH=. python scripts/poc_rp_planvoorraad.py 0828   # Oss
"""

import json
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import httpx

from src.config import cfg

# Velden waar we expliciet naar zoeken voor het snapshot-schema.
WANTED = [
    "id", "identificatie", "type", "planstatus", "dossierstatus", "dossier",
    "naam", "planstatusInfo", "verwijzingNaarExternBestand",
    "publicatiedatum", "vaststellingsdatum", "datum", "datumWijziging",
    "eindeRechtsgeldigheid", "isTamPlan", "bronhouder", "leverancier",
    "verwerkingssoftware", "software", "_links",
]


def _headers():
    # DSO Ruimtelijke Plannen API: HAL+JSON, en geometrie-endpoints willen een CRS.
    return {
        "X-Api-Key": cfg.IHR_API_KEY,
        "Accept": "application/hal+json",
        "Accept-Crs": "epsg:28992",
        "Content-Crs": "epsg:28992",
    }


def _walk_keys(obj, prefix=""):
    """Vlakke lijst van alle (geneste) sleutelpaden in een dict."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}{k}"
            out.append(path)
            if isinstance(v, (dict, list)):
                out.extend(_walk_keys(v, prefix=f"{path}."))
    elif isinstance(obj, list) and obj:
        out.extend(_walk_keys(obj[0], prefix=prefix))
    return out


def fetch_sample(gemeentecode: str | None):
    """Probeer een handvol plannen op te halen. Eerst /plannen (gepagineerd),
    daarna /_zoek met een bronhouder-filter als dat nodig blijkt."""
    base = cfg.IHR_BASE.rstrip("/")

    # --- poging 1: GET /plannen ---
    params = {"pageSize": 5, "page": 1}
    print(f"[1] GET {base}/plannen  params={params}")
    try:
        r = httpx.get(f"{base}/plannen", headers=_headers(), params=params, timeout=30)
        print(f"    HTTP {r.status_code}")
        if r.status_code == 200:
            return r.json(), "GET /plannen"
        print(f"    body: {r.text[:300]}")
    except Exception as e:
        print(f"    fout: {e}")

    # --- poging 2: GET /plannen met alleStatussen=true ---
    params2 = {"pageSize": 5, "page": 1, "alleStatussen": "true"}
    print(f"[2] GET {base}/plannen  params={params2}")
    try:
        r = httpx.get(f"{base}/plannen", headers=_headers(), params=params2, timeout=30)
        print(f"    HTTP {r.status_code}")
        if r.status_code == 200:
            return r.json(), "GET /plannen?alleStatussen=true"
        print(f"    body: {r.text[:300]}")
    except Exception as e:
        print(f"    fout: {e}")

    # --- poging 3: POST /_zoek (zoek op alle hoofdcollecties) ---
    body = {"zoekVelden": []}
    print(f"[3] POST {base}/plannen/_zoek  body={body}")
    try:
        r = httpx.post(f"{base}/plannen/_zoek", headers=_headers(),
                       json=body, params={"pageSize": 5}, timeout=30)
        print(f"    HTTP {r.status_code}")
        if r.status_code == 200:
            return r.json(), "POST /plannen/_zoek"
        print(f"    body: {r.text[:300]}")
    except Exception as e:
        print(f"    fout: {e}")

    return None, None


def main():
    gemeentecode = sys.argv[1] if len(sys.argv) > 1 else None

    if not cfg.IHR_API_KEY:
        print("IHR_API_KEY niet gezet in .env — kan niet bevragen.")
        sys.exit(1)

    print(f"IHR_BASE = {cfg.IHR_BASE}")
    print(f"API-key aanwezig: {'ja' if cfg.IHR_API_KEY else 'nee'}\n")

    data, via = fetch_sample(gemeentecode)
    if not data:
        print("\nGeen 200-respons gekregen. Zie pogingen hierboven.")
        sys.exit(2)

    print(f"\n=== Respons via: {via} ===")
    print(f"top-level keys: {list(data.keys())}")

    page = data.get("page")
    if page:
        print(f"page: {page}")

    emb = data.get("_embedded", {})
    coll = None
    plannen = []
    if isinstance(emb, dict) and emb:
        coll = next(iter(emb.keys()))
        plannen = emb[coll] or []
    print(f"_embedded collectie: {coll!r}, aantal in sample: {len(plannen)}")

    if not plannen:
        print("Geen plannen in sample; rauwe respons (ingekort):")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
        return

    plan = plannen[0]
    print("\n--- Eerste plan: alle sleutelpaden ---")
    keys = _walk_keys(plan)
    for k in keys:
        print(f"  {k}")

    print("\n--- Aanwezigheid van gezochte velden ---")
    flat = set(keys)
    top = set(plan.keys())
    for w in WANTED:
        hit = w in top or any(p == w or p.endswith(f".{w}") for p in flat)
        print(f"  {'OK ' if hit else '-- '} {w}")

    print("\n--- Eerste plan (ingekort) ---")
    print(json.dumps(plan, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
