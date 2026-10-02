"""Exporteer de sync-historie naar runs.json voor elconteo.nl/omgevingswet/data-actualiteit.

Leest audit.sync_run (totalen + metrics per run) en core.load_run (de fasen met
tijden en aantallen) uit de lokale werkbank en schrijft één JSON-bestand dat de
publieke pagina rendert. Geen API-sleutel, geen CORS: de site is statisch, dus
dit bestand gaat mee in de deploy van Conteo_website.

Gebruik (runbook stap 8/9, na elke sync):

    python scripts/export_data_actualiteit.py              # schrijft naar de site-repo
    python scripts/export_data_actualiteit.py --uit pad.json
    python scripts/export_data_actualiteit.py --droog      # toon samenvatting, schrijf niets

Daarna in c:/GIT/Conteo_website committen en pushen (push = deploy).

Koppeling sync-run ↔ fasen: core.load_run kent geen sync-run-id. Een fase hoort
bij de sync-run waarin hij startte: started_at >= gestart_op van die run, vóór de
eerstvolgende run én binnen 24 uur na de start. Het venster loopt bewust voorbij
klaar_op van full_sync (stappen 6 t/m 8 gaan daarna nog uren door), maar niet
tot de volgende sync: los werk tussen twee syncs (Wro, opschoning) hoort bij
geen van beide.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

STANDAARD_UIT = Path("c:/GIT/Conteo_website/public/omgevingswet/data-actualiteit/runs.json")

# Publieke namen per bron-sleutel. 'knoop' = het knooppunt op de stroomkaart.
BRONNEN = {
    "ozon-regelingen":         {"label": "omgevingsdocumenten",                 "knoop": "ozon", "primair": True},
    "ozon-ontwerpen":          {"label": "ontwerpen",                            "knoop": "ozon"},
    "ozon-besluitversies":     {"label": "besluitversies",                       "knoop": "ozon"},
    "ozon-afwijkvergunningen": {"label": "afwijkvergunningen (DSO)",             "knoop": "ozon"},
    "rtr-toepasbare-regels":   {"label": "toepasbare regels",                    "knoop": "rtr",  "primair": True},
    "koop-sru-vergunningen":   {"label": "vergunningkennisgevingen",             "knoop": "koop", "primair": True},
    "obk-vergunningen-inhoud": {"label": "kennisgevingen met volledige inhoud",  "knoop": "koop"},
    "pdok-bestemmingsplannen": {"label": "bestemmingsplannen (Wro)",             "knoop": "pdok", "primair": True},
    "pdok-structuurvisies":    {"label": "structuurvisies (Wro)",                "knoop": "pdok"},
    "ihr-plannen":             {"label": "plantekst-objecten (Wro)",             "knoop": "pdok"},
    "ihr-planvoorraad":        {"label": "plannen in de planvoorraad (Wro)",     "knoop": "pdok"},
    "pdok-gemeentegrenzen":    {"label": "gemeentegrenzen",                      "knoop": "pdok"},
    "mer-commissie":           {"label": "MER-projecten (Commissie m.e.r.)",     "knoop": "mer",  "primair": True},
    "mer-events":              {"label": "MER-gebeurtenissen (bekendmakingen)",  "knoop": "mer"},
    "v2a-tekst-embedding":     {"label": "tekstfragmenten in de vectorindex (netto, na opruimen)", "knoop": "hub", "primair": True},
    "v2a-chunk-annotatie":     {"label": "fragment-annotaties",                  "knoop": "hub"},
    "v2a-chunk-categorie":     {"label": "fragment-onderwerpen",                 "knoop": "hub"},
}

# Publieke namen van de fasen (core.load_run.bron) voor het tijdverloop.
FASEN = {
    "ozon-regelingen":         "omgevingsdocumenten laden",
    "ozon-ontwerpen":          "ontwerpen bijhouden",
    "ozon-besluitversies":     "besluitversies bijhouden",
    "ozon-afwijkvergunningen": "afwijkvergunningen laden",
    "rtr-toepasbare-regels":   "toepasbare regels laden",
    "koop-sru-vergunningen":   "vergunningen laden en verrijken",
    "obk-vergunningen-inhoud": "publicatie-inhoud ophalen",
    "locatie-subdiv":          "geometrie opknippen",
    "locatie-generalisatie":   "geometrie vereenvoudigen",
    "post-processing":         "ponsenkaart-statistieken",
    "indeling":                "onderwerp-indeling",
    "drieslag-mv":             "koppelingen herbouwen",
    "health-mv":               "gezondheidsmetingen",
    "v2a-tekst-embedding":     "vectorindex bijwerken",
    "v2a-chunk-annotatie":     "fragment-annotaties",
    "v2a-chunk-categorie":     "fragment-onderwerpen",
    "mer-events":              "MER-gebeurtenissen",
    "mer-commissie":           "MER-projecten",
    "pdok-bestemmingsplannen": "bestemmingsplannen (Wro)",
    "pdok-structuurvisies":    "structuurvisies (Wro)",
    "ihr-plannen":             "plantekst (Wro)",
    "ihr-planvoorraad":        "planvoorraad (Wro)",
    "pdok-gemeentegrenzen":    "gemeentegrenzen",
    "prune-verouderd":         "verouderde versies opschonen",
    "naammatch-migratie":      "naammatch-migratie",
}


def dsn() -> str:
    return "host={} port={} dbname={} user={} password={}".format(
        os.environ.get("DB_HOST", "localhost"), os.environ.get("DB_PORT", "5434"),
        os.environ.get("DB_NAME", "dso"), os.environ.get("DB_USER", "postgres"),
        os.environ.get("DB_PASSWORD", "postgres"))


def iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def seconden(a: datetime | None, b: datetime | None) -> int | None:
    return int((b - a).total_seconds()) if a and b else None


def db_gb(metrics: dict | None) -> float | None:
    """'97 GB' → 97.0; '1024 MB' → 1.0; anders None."""
    s = (metrics or {}).get("db_grootte")
    if not s:
        return None
    try:
        getal, eenheid = s.split()
        getal = float(getal.replace(",", "."))
        return round(getal / 1024, 1) if eenheid.upper().startswith("M") else getal
    except ValueError:
        return None


def exporteer(conn: psycopg.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("""
        SELECT run_id, label, gestart_op, klaar_op, opmerking, totalen, metrics
        FROM audit.sync_run ORDER BY gestart_op
    """)
    syncs = cur.fetchall()
    if not syncs:
        raise SystemExit("audit.sync_run is leeg")

    eerste = syncs[0]["gestart_op"]
    cur.execute("""
        SELECT run_id, bron, scope, started_at, finished_at, status, n_verwerkt, n_fout, details
        FROM core.load_run WHERE started_at >= %s ORDER BY started_at
    """, (eerste,))
    fasen = cur.fetchall()

    runs = []
    vorige_totalen: dict | None = None
    for i, s in enumerate(syncs):
        venster_tot = s["gestart_op"] + timedelta(hours=24)
        if i + 1 < len(syncs):
            venster_tot = min(venster_tot, syncs[i + 1]["gestart_op"])
        eigen = [f for f in fasen if s["gestart_op"] <= f["started_at"] < venster_tot]
        totalen = s["totalen"]
        delta = None
        if totalen and vorige_totalen:
            delta = {k: totalen[k] - vorige_totalen[k]
                     for k in totalen if k in vorige_totalen
                     and isinstance(totalen[k], int) and isinstance(vorige_totalen[k], int)}
        afgebroken = totalen is None
        baseline = (s["label"] or "").startswith("baseline")
        fouten = sum(f["n_fout"] or 0 for f in eigen)
        laatste_fase = max((f["finished_at"] for f in eigen if f["finished_at"]), default=None)
        runs.append({
            "run": s["run_id"],
            "label": s["label"],
            "gestart": iso(s["gestart_op"]),
            "klaar": iso(s["klaar_op"]),
            "laatste_fase_klaar": iso(laatste_fase),
            "duur_s": seconden(s["gestart_op"], laatste_fase or s["klaar_op"]),
            "status": "baseline" if baseline else "afgebroken" if afgebroken else "ok",
            "opmerking": s["opmerking"],
            "fouten": fouten,
            "totalen": totalen,
            "delta": delta,
            "verwacht": (s["metrics"] or {}).get("verwacht"),
            "db_gb": db_gb(s["metrics"]),
            "regelingen_inactief": (s["metrics"] or {}).get("regelingen_inactief"),
            "fasen": [{
                "bron": f["bron"],
                "naam": FASEN.get(f["bron"], f["bron"]),
                "scope": f["scope"],
                "start": iso(f["started_at"]),
                "einde": iso(f["finished_at"]),
                "duur_s": seconden(f["started_at"], f["finished_at"]),
                "status": f["status"],
                "verwerkt": f["n_verwerkt"],
                "fouten": f["n_fout"],
                "voor": (f["details"] or {}).get("totaal_voor"),
                "na": (f["details"] or {}).get("totaal_na"),
            } for f in eigen],
        })
        if totalen:
            vorige_totalen = totalen

    return {
        "gegenereerd": iso(datetime.now(timezone.utc)),
        "toelichting": "Sync-historie van de OCD-werkbank: per run de totaalstand per bron, "
                       "het verschil met de vorige voltooide run, en de fasen met hun tijden. "
                       "Gegenereerd door dso-loader/scripts/export_data_actualiteit.py.",
        "bronnen": BRONNEN,
        "runs": runs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--uit", type=Path, default=STANDAARD_UIT, help="doelbestand (standaard: de site-repo)")
    ap.add_argument("--droog", action="store_true", help="alleen samenvatting tonen")
    args = ap.parse_args()

    with psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=10) as conn:
        data = exporteer(conn)

    voltooid = [r for r in data["runs"] if r["status"] == "ok"]
    laatste = voltooid[-1] if voltooid else None
    print(f"{len(data['runs'])} runs ({len(voltooid)} voltooid), "
          f"{sum(len(r['fasen']) for r in data['runs'])} fasen")
    if laatste:
        d = laatste["delta"] or {}
        print(f"laatste: run {laatste['run']} {laatste['label']} - "
              f"{d.get('ozon-regelingen', 0):+d} regelingen - "
              f"{d.get('koop-sru-vergunningen', 0):+d} kennisgevingen - {laatste['fouten']} fouten")
    if args.droog:
        return 0

    args.uit.parent.mkdir(parents=True, exist_ok=True)
    args.uit.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"geschreven: {args.uit} ({args.uit.stat().st_size // 1024} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
