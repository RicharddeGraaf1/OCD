"""Snelle regelingen-diff op EXPRESSIE-niveau via de algemene /regelingen-call.

Vervangt de trage per-bronhouder-crawl (diff_dso_bronhouder_coverage.py) voor het
detecteren van missende én stil gewijzigde regelingen. DSO telt in totaal maar
~1930 regelingen (10 pagina's van 200), dus één gepagineerde scan zonder
bevoegdGezag-filter volstaat — seconden i.p.v. honderden seriële calls.

Cruciaal: dit vergelijkt op **expressie** (versie), niet op work. Zo komen ook
nieuwe versies van bestaande omgevingsplannen naar boven — die de work-diff mist
(daar "heb je het plan al").

Usage (invocatie-onafhankelijk; zet zelf de repo-root op sys.path):
    python scripts/diff_regelingen_snel.py
    python scripts/diff_regelingen_snel.py --codes-out drift.txt
        -> schrijft 'overheidscode,naam' per geraakte bronhouder (voor
           herladen met: while read bh; do python -m src.cli load-api -o "$bh"; done)
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from src.config import cfg  # noqa: E402
from src.db import get_conn  # noqa: E402
from src.loaders.ow_loader import _api_headers  # noqa: E402


def fetch_dso_expressions() -> dict[str, str | None]:
    """Alle Ow-regeling-expressies in DSO → {expressionId: bronhouder-code}."""
    base = cfg.PRESENTEREN_BASE
    headers = _api_headers()
    dso: dict[str, str | None] = {}
    page = 1
    while True:
        resp = httpx.get(f"{base}/regelingen", headers=headers,
                         params={"page": page, "size": 200}, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        regs = data.get("_embedded", {}).get("regelingen", [])
        if not regs:
            break
        for reg in regs:
            dso[reg["expressionId"]] = reg.get("aangeleverdDoorEen", {}).get("code")
        if not data.get("_links", {}).get("next", {}).get("href"):
            break
        page += 1
    return dso


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes-out",
                    help="Schrijf 'overheidscode,naam' van geraakte bronhouders naar dit bestand.")
    args = ap.parse_args()

    dso = fetch_dso_expressions()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT frbr_expression FROM p2p.regeling")
            lokaal = {r["frbr_expression"] for r in cur.fetchall()}

        mist = [e for e in dso if e not in lokaal]
        over = [e for e in lokaal if e not in dso]
        per_bh = Counter(dso[e] for e in mist)

        print(f"DSO: {len(dso)} expressies | lokaal: {len(lokaal)}")
        print(f"MIST (in DSO, niet lokaal): {len(mist)}  |  "
              f"OVER (lokaal, niet in DSO): {len(over)}")
        print(f"geraakte bronhouders: {len(per_bh)}")
        for code, n in per_bh.most_common():
            print(f"  {code}: {n}")

        if args.codes_out:
            codes = list(per_bh)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT overheidscode, coalesce(naam, overheidscode) AS naam "
                    "FROM core.bronhouder WHERE overheidscode = ANY(%s)", (codes,))
                naam = {r["overheidscode"]: r["naam"] for r in cur.fetchall()}
            with open(args.codes_out, "w", encoding="utf-8") as f:
                for code in codes:
                    f.write(f"{code},{naam.get(code, code)}\n")
            print(f"geschreven: {len(codes)} bronhouders -> {args.codes_out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
