"""Stap 6bis, deel 2 — tel de subagent-uitvoer na en laad hem in v2a.hertaling.

De natelling is het punt van dit script, niet het laden. Op 2026-08-16 leverden
twee van de elf subagents minder dan ze rapporteerden: een meldde zelf 118 van
122, maar een andere meldde 122 regels "gevalideerd" terwijl een hash daarin in
de invoer niet voorkwam — verzonnen sleutel, echte tekst zonder hertaling. Het
eindrapport van een agent is dus geen bewijs; de sleutelset is dat wel.

Gecontroleerd wordt daarom per `bron_hash` uit de invoer: is er precies een
niet-lege regel terug, is die regel geldige JSON, en komt de hash uit de invoer.
Ontbreekt er iets, dan schrijft dit script `batch-herstel.json` en stopt het met
exitcode 1 — draai daar een extra subagent op (uitvoer `out-herstel.jsonl`) en
herhaal. Met `--deels` laadt hij toch wat er is.

Run:  python scripts/hertaal_fanout_laad.py            # alleen natellen
      python scripts/hertaal_fanout_laad.py --ja       # natellen + laden
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

# Voortgangsregels bevatten een → (U+2192). Op een cp1252-console gooit dat een
# UnicodeEncodeError — en als dat ná de commit gebeurt, lijkt het laden mislukt
# terwijl de data er gewoon staat. Gebeurd 2026-08-22 in dit script en in
# repliceer_p2p_naar_prod.py, allebei op dezelfde dag.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HIER = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(HIER / ".env")

MIN_HERTALING = 25  # korter is vrijwel zeker een afgekapte of lege uitvoer


def db_url() -> str:
    return (f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(HIER / "data" / "hertaal"))
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--prompt-versie", default="v1")
    ap.add_argument("--ja", action="store_true", help="echt laden")
    ap.add_argument("--deels", action="store_true", help="laden ook als er teksten ontbreken")
    args = ap.parse_args()

    werk = pathlib.Path(args.dir)
    verwacht: dict[str, str] = {}
    for pad in sorted(werk.glob("batch-*.json")):
        if pad.name == "batch-herstel.json":
            continue
        for r in json.loads(pad.read_text(encoding="utf-8")):
            verwacht[r["bron_hash"]] = r["tekst"]
    if not verwacht:
        sys.exit(f"geen batch-*.json in {werk} — draai eerst hertaal_fanout_export.py")

    gekregen: dict[str, str] = {}
    kapot, leeg, kort, onbekend = [], [], [], []
    for pad in sorted(werk.glob("out-*.jsonl")):
        for nr, regel in enumerate(pad.read_text(encoding="utf-8").splitlines(), 1):
            regel = regel.strip()
            if not regel:
                continue
            try:
                o = json.loads(regel)
            except Exception as e:                                    # noqa: BLE001
                kapot.append(f"{pad.name}:{nr} {str(e)[:50]}")
                continue
            h, t = o.get("bron_hash"), (o.get("tekst") or "").strip()
            if not h:
                kapot.append(f"{pad.name}:{nr} geen bron_hash")
            elif h not in verwacht:
                onbekend.append(f"{pad.name}:{nr} {h[:12]}")
            elif not t:
                leeg.append(h)
            else:
                if len(t) < MIN_HERTALING:
                    kort.append(h)
                gekregen[h] = t

    ontbreekt = [h for h in verwacht if h not in gekregen]
    print(f"verwacht {len(verwacht)} · gedekt {len(gekregen)} · ontbreekt {len(ontbreekt)}")
    for naam, lijst in (("kapotte regels", kapot), ("hash niet in invoer", onbekend),
                        ("lege hertalingen", leeg), (f"korter dan {MIN_HERTALING} tekens", kort)):
        if lijst:
            print(f"  {naam}: {len(lijst)}  {lijst[:3]}")

    if ontbreekt:
        herstel = werk / "batch-herstel.json"
        herstel.write_text(json.dumps(
            [{"bron_hash": h, "tekst": verwacht[h]} for h in ontbreekt],
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n{len(ontbreekt)} teksten zonder hertaling → {herstel}")
        print("Draai daar een subagent op (uitvoer out-herstel.jsonl) en herhaal.")
        if not args.deels:
            sys.exit(1)

    if not args.ja:
        print("\ndroogloop — draai opnieuw met --ja om te laden")
        return

    rijen = [(h, args.model, args.prompt_versie, t) for h, t in gekregen.items()]
    with psycopg.connect(db_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2a.hertaling")
        voor = cur.fetchone()[0]
        cur.executemany(
            """INSERT INTO v2a.hertaling (bron_hash, model, prompt_versie, tekst)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (bron_hash, model, prompt_versie)
               DO UPDATE SET tekst = EXCLUDED.tekst, gegenereerd_op = now()""", rijen)
        conn.commit()
        cur.execute("SELECT count(*) FROM v2a.hertaling")
        na = cur.fetchone()[0]
    print(f"\ncache {voor} → {na} (+{na - voor}, aangeboden {len(rijen)})")
    print("Daarna naar prod: powershell -File scripts/sync-hertaling-to-prod.ps1 -All -ProdUrl ...")


if __name__ == "__main__":
    main()
