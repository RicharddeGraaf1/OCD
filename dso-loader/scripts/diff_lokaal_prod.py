#!/usr/bin/env python
"""Tel élke tabel aan beide kanten en meld alleen wat onverwacht verschilt.

Waarom dit bestaat
------------------
De sync van 2026-08-28 vond twee gaten die geen enkele bestaande controle zag:
`p2p.tekstdeel_hoofdlijn` stond op prod op **0** tegen 4.955 lokaal, en van de
38 ontbrekende gebiedsaanwijzingen ontbrak ook de locatie eronder. Beide waren
alleen zichtbaar door met de hand te tellen. Dit script maakt van dat handwerk
een stap.

Het meet niet of een fase gedraaid heeft — dat doet de regressiecheck — maar of
de twee databases hetzelfde bevatten. Dat is een andere vraag, en het is de vraag
waar prod stil onvolledig van wordt.

De verwachtingen
----------------
Sommige verschillen horen er te zijn. Die staan in `diff_verwachtingen.yml` met
een reden erbij, zodat de uitvoer leeg is als alles klopt. Een controle die altijd
ruis geeft, wordt niet gelezen — precies de reden dat "0 fouten" jarenlang valse
geruststelling gaf.

Verwachtingen zijn **richtingsgevoelig** en hebben een marge, zodat een gat dat
groeit alsnog opvalt. Wie een verwachting toevoegt zonder reden krijgt een
waarschuwing.

    python scripts/diff_lokaal_prod.py            # alleen de verschillen
    python scripts/diff_lokaal_prod.py --alles    # ook wat gelijk is
    python scripts/diff_lokaal_prod.py --json     # machineleesbaar

Exitcode 0 = geen onverwacht verschil, 1 = wel, 2 = kon niet meten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
import yaml
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
VERWACHTINGEN = Path(__file__).resolve().parent / "diff_verwachtingen.yml"

# Schema's die aan beide kanten gelijk horen te zijn. Bewust niet:
#   audit    — lokale run-historie; prod heeft een eigen, kortere reeks
#   vangnet  — lokaal veiligheidsnet van stap 10, bestaat alleen hier
#   conv/lev — werkschema's van conversie-experimenten, niet gerepliceerd
#   public   — postgis-restant
SCHEMAS = ["core", "p2p", "p2pwijziging", "v2a", "i2a", "irm", "mer", "vth",
           "wro", "skos"]


def lokale_dsn() -> str:
    return (f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")


def tabelnamen(cur, schemas: list[str]) -> list[str]:
    cur.execute("""
        SELECT table_schema, table_name FROM information_schema.tables
         WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s)
         ORDER BY table_schema, table_name""", (schemas,))
    return [f"{s}.{t}" for s, t in cur.fetchall()]


def tel_alles(dsn: str, schemas: list[str], label: str,
              stil: bool = False) -> dict[str, int]:
    """Exacte rijtelling per tabel.

    Exact, geen `reltuples`-schatting: het gat dat dit script moet vinden was
    4.955 tegen 0, en juist bij zulke tabellen is de schatting het slechtst —
    `v2a.tekst_embedding` stond in de statistieken op 0 terwijl er 1,65 miljoen
    rijen in zaten (vault G-133).

    Eén `UNION ALL` over honderd tabellen leek sneller maar is het niet: hij
    dwingt de hele reeks in één transactie en gaf op 92 GB geen antwoord binnen
    tien minuten. Per tabel tellen kost evenveel werk maar toont voortgang, en
    een tabel die omvalt houdt de rest niet op. De twee kanten draaien parallel,
    dus de wandklok is die van de traagste kant en niet de som.
    """
    uit: dict[str, int] = {}
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET statement_timeout = '15min'")
        namen = tabelnamen(cur, schemas)
        for i, tabel in enumerate(namen, 1):
            try:
                cur.execute(f"SELECT count(*) FROM {tabel}")
                uit[tabel] = cur.fetchone()[0]
            except Exception as e:
                conn.rollback()
                print(f"  [{label}] {tabel}: telling mislukt ({str(e)[:60]})",
                      file=sys.stderr, flush=True)
            if not stil and i % 25 == 0:
                print(f"  [{label}] {i}/{len(namen)} tabellen geteld",
                      file=sys.stderr, flush=True)
    return uit



def laad_verwachtingen() -> dict:
    if not VERWACHTINGEN.exists():
        return {}
    with VERWACHTINGEN.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("verwacht", {}) or {}


def beoordeel(tabel: str, lok: int | None, prod: int | None, verwacht: dict):
    """→ (status, toelichting). status ∈ gelijk | verwacht | AFWIJKING | ONTBREEKT."""
    if lok is None or prod is None:
        # Een tabel die maar aan één kant bestaat kon aanvankelijk nooit
        # "verwacht" worden, waardoor het script na de eerste triage nog altijd
        # elf afwijkingen meldde -- precies de ruis waar de kop van dit bestand
        # voor waarschuwt. `alleen: lokaal` / `alleen: prod` in het
        # verwachtingenbestand dekt dat af, en meldt het alsnog als de tabel aan
        # de verkeerde kant opduikt.
        kant = "prod" if prod is None else "lokaal"
        aanwezig = "lokaal" if prod is None else "prod"
        v = verwacht.get(tabel) or {}
        if v.get("alleen") == aanwezig:
            return "verwacht", v.get("reden", "(geen reden opgegeven — vul aan)")
        if v.get("alleen"):
            return ("AFWIJKING",
                    f"verwacht alleen aan de {v['alleen']}-kant, maar staat nu "
                    f"alleen aan de {aanwezig}-kant")
        return "ONTBREEKT", f"tabel bestaat niet aan de {kant}-kant"
    verschil = lok - prod
    if verschil == 0:
        return "gelijk", ""
    v = verwacht.get(tabel)
    if not v:
        return "AFWIJKING", f"{verschil:+,} onverklaard"
    ondergrens, bovengrens = v.get("min", v.get("verschil")), v.get("max", v.get("verschil"))
    if ondergrens is None or bovengrens is None:
        return "AFWIJKING", "verwachting zonder min/max in diff_verwachtingen.yml"
    if ondergrens <= verschil <= bovengrens:
        return "verwacht", v.get("reden", "(geen reden opgegeven — vul aan)")
    return ("AFWIJKING",
            f"{verschil:+,} valt buiten de verwachte marge [{ondergrens:+,}, {bovengrens:+,}] — "
            f"{v.get('reden', '')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--alles", action="store_true", help="toon ook de gelijke tabellen")
    ap.add_argument("--json", action="store_true", help="machineleesbare uitvoer")
    ap.add_argument("--schema", action="append", help="beperk tot deze schema's")
    a = ap.parse_args()

    prod_dsn = os.getenv("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2
    schemas = a.schema or SCHEMAS

    # Beide kanten tegelijk: het zijn twee losse servers en de traagste bepaalt
    # de wandklok. Sequentieel duurde dit ruim het dubbele.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_lok = pool.submit(tel_alles, lokale_dsn(), schemas, "lokaal", a.json)
            f_prod = pool.submit(tel_alles, prod_dsn, schemas, "prod", a.json)
            lokaal, prod = f_lok.result(), f_prod.result()
    except Exception as e:
        print(f"kon niet meten: {e}", file=sys.stderr)
        return 2

    verwacht = laad_verwachtingen()
    rijen = []
    for tabel in sorted(set(lokaal) | set(prod)):
        lok, pr = lokaal.get(tabel), prod.get(tabel)
        status, toelichting = beoordeel(tabel, lok, pr, verwacht)
        rijen.append({"tabel": tabel, "lokaal": lok, "prod": pr,
                      "status": status, "toelichting": toelichting})

    afwijkend = [r for r in rijen if r["status"] in ("AFWIJKING", "ONTBREEKT")]

    if a.json:
        print(json.dumps({"tabellen": len(rijen), "afwijkend": len(afwijkend),
                          "rijen": rijen if a.alles else afwijkend},
                         ensure_ascii=False, indent=2))
        return 1 if afwijkend else 0

    toon = rijen if a.alles else [r for r in rijen if r["status"] != "gelijk"]
    if toon:
        print(f"{'tabel':<40} {'lokaal':>12} {'prod':>12}  status")
        print("-" * 92)
        for r in toon:
            lok = "—" if r["lokaal"] is None else f"{r['lokaal']:,}"
            pr = "—" if r["prod"] is None else f"{r['prod']:,}"
            print(f"{r['tabel']:<40} {lok:>12} {pr:>12}  {r['status']}")
            if r["toelichting"]:
                print(f"{'':<40} {'':>12} {'':>12}  ↳ {r['toelichting']}")
        print()

    gelijk = sum(1 for r in rijen if r["status"] == "gelijk")
    verw = sum(1 for r in rijen if r["status"] == "verwacht")
    print(f"{len(rijen)} tabellen · {gelijk} gelijk · {verw} verwacht verschil · "
          f"{len(afwijkend)} AFWIJKEND")
    if afwijkend:
        print("\nEen afwijking is niet automatisch een fout, maar wél iets om te verklaren.")
        print("Klopt hij en hoort hij er te zijn, zet hem dan met reden in "
              f"{VERWACHTINGEN.name}.")
    return 1 if afwijkend else 0


if __name__ == "__main__":
    raise SystemExit(main())
