#!/usr/bin/env python
"""Herhaal de i2a-fase voor een handvol bronhouders die tijdens een sync omvielen.

Waarom dit nodig was
--------------------
`python -m src.cli load-imtr` laadt **alleen de POC-bronhouder** uit `.env`
(POC_OIN — Utrecht), niet landelijk. De landelijke sweep zit in
`full_sync.fase_i2a` en heeft geen filter. Wie na een sync een paar gestrande
gemeenten wil herstellen had dus de keuze tussen "de hele fase opnieuw" of
"niets" — gemeten 2026-08-28, toen acht bronhouders na vijf retries alsnog op
een 503 omvielen.

Dit script neemt dezelfde ingang (`i2a.run`) met een gefilterde bronhouderlijst.
De werkzaamhedencatalogus blijft eruit: die is landelijk en in de sync al
geladen.

Dat het werkt is meteen ook de diagnose. Op 2026-08-28 kwamen alle acht er in
**45 seconden** doorheen, dus de 503's waren transiënte DSO-hikken en geen
storing — precies zoals de Nieuwegein-503 van 2026-08-01.

    python scripts/i2a_herstel_bronhouders.py 0394 0753 0880
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn                       # noqa: E402
from src.pipeline import i2a                      # noqa: E402
from src.pipeline.bronhouders import Bronhouder   # noqa: E402
from src.run_log import load_run                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+",
                    help="kale CBS-codes zonder gm-prefix, zoals de sync ze meldt (0394 0753 …)")
    a = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT overheidscode, naam FROM core.gemeentegrens ORDER BY overheidscode")
    namen = {r["overheidscode"][2:]: r["naam"] for r in cur.fetchall()}
    conn.close()

    onbekend = [c for c in a.codes if c not in namen]
    if onbekend:
        print(f"onbekende code(s): {', '.join(onbekend)}", file=sys.stderr)
        return 2

    lijst = [Bronhouder(code=c, naam=namen[c], type="gemeente") for c in a.codes]
    print("herstel voor:", ", ".join(f"{b.code} {b.naam}" for b in lijst))

    with load_run("rtr-toepasbare-regels", scope="herstel:losse bronhouders") as run:
        res = i2a.run(lijst, load_werkzaamheden=False)
        fout = {k: v for k, v in res.items() if v != "ok"}
        run.set(n_fout=len(fout))

    for code, uitkomst in res.items():
        print(f"  {code}: {uitkomst if uitkomst == 'ok' else uitkomst[:80]}")
    return 1 if fout else 0


if __name__ == "__main__":
    raise SystemExit(main())
