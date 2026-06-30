"""PoC G-82: hoe diep kunnen we backfillen via `verwijderdOp`?

Paginereert door /plannen (planType=bestemmingsplan), volgt HAL `_links.next`,
en telt de spreiding van `verwijderdOp`-datums. Doel: bepalen of de API
verwijderde plannen lang genoeg blijft serveren om de tijd-as met terugwerkende
kracht te vullen (diep venster) of niet (smal venster → forward-only nodig).

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate
    PYTHONPATH=. python scripts/poc_rp_verwijderdop.py [MAX_PAGES]
"""

import os
import sys
import time
from collections import Counter

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx

from src.config import cfg

H = {"X-Api-Key": cfg.IHR_API_KEY, "Accept": "application/hal+json",
     "Accept-Crs": "epsg:28992", "Content-Crs": "epsg:28992"}


def main():
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    base = cfg.IHR_BASE.rstrip("/")

    url = f"{base}/plannen"
    params = {"planType": "bestemmingsplan", "pageSize": 100, "page": 1}

    total = 0
    verwijderd = 0
    tam = 0
    by_month = Counter()
    oldest = None
    newest = None
    first_links = None
    pages = 0

    while url and pages < max_pages:
        try:
            r = httpx.get(url, headers=H, params=params if pages == 0 else None, timeout=60)
        except Exception as e:
            print(f"fout op pagina {pages+1}: {e}")
            break
        if r.status_code != 200:
            print(f"HTTP {r.status_code} op pagina {pages+1}: {r.text[:200]}")
            break
        d = r.json()
        plannen = d.get("_embedded", {}).get("plannen", []) or []
        links = d.get("_links", {}) or {}
        if pages == 0:
            first_links = list(links.keys())
        for p in plannen:
            total += 1
            if p.get("isTamPlan"):
                tam += 1
            vo = p.get("verwijderdOp")
            if vo:
                verwijderd += 1
                ym = vo[:7]  # YYYY-MM
                by_month[ym] += 1
                if oldest is None or vo < oldest:
                    oldest = vo
                if newest is None or vo > newest:
                    newest = vo
        pages += 1
        nxt = links.get("next", {})
        url = nxt.get("href") if isinstance(nxt, dict) else None
        if not plannen:
            break
        time.sleep(0.2)

    print(f"Pagina's doorlopen : {pages}")
    print(f"_links op pagina 1 : {first_links}")
    print(f"Plannen gezien     : {total}")
    print(f"waarvan TAM-plannen: {tam}")
    print(f"met verwijderdOp   : {verwijderd}  ({(verwijderd/total*100 if total else 0):.1f}%)")
    print(f"verwijderdOp-bereik: {oldest}  ..  {newest}")
    print("\nverwijderdOp per maand (oplopend):")
    for ym in sorted(by_month):
        print(f"  {ym}: {by_month[ym]}")

    print("\nDuiding:")
    if oldest and oldest[:7] <= "2025-01":
        print("  → DIEP venster: verwijderde plannen van >1 jaar terug zijn nog opvraagbaar.")
        print("    Backfill van de tijd-as met terugwerkende kracht is grotendeels mogelijk.")
    elif oldest:
        print("  → mogelijk SMAL venster: oudste verwijderdOp is relatief recent.")
        print("    Forward-only + maandelijkse snapshots blijven nodig (let op pagina-bias).")
    else:
        print("  → geen verwijderdOp in deze trek; vergroot MAX_PAGES of check sortering.")


if __name__ == "__main__":
    main()
