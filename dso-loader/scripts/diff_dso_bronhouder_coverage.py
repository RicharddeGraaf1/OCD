"""Diff DSO Presenteren ↔ lokale DB per bronhouder.

Vraagt /regelingen?bevoegdGezag=<code> bij DSO en vergelijkt de
identificaties (frbr_work) met p2p.regeling lokaal. Per bronhouder
rapporteert het script hoeveel regelingen ontbreken (in DSO maar
niet lokaal) of overbodig zijn (lokaal maar niet meer in DSO).

Detectie-only — laden gebeurt apart, bv via `loaders.api_loader.load_via_api`
of een lokaal hersteld-script.

Gebruik:
    python scripts/diff_dso_bronhouder_coverage.py
    python scripts/diff_dso_bronhouder_coverage.py --prefix pv,mn,ws
    python scripts/diff_dso_bronhouder_coverage.py --codes pv25,ws0653 --details
    python scripts/diff_dso_bronhouder_coverage.py --limit 20 --details
"""

import argparse
import sys
import os
import time
from collections import defaultdict

os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _get

console = Console()


def dso_voor_bronhouder(code: str) -> list[dict] | None:
    """Regelingen die DSO meldt voor deze bronhouder.

    Retourneert `None` als ÉÉN of meer pagina's na alle retries blijven
    falen — anders zou een 5xx op page 1 als "DSO is leeg" worden uitgelegd
    en zou alles lokaal als 'overbodig' worden gerapporteerd.
    """
    out: dict[str, dict] = {}
    for page in range(1, 500):
        data = None
        for poging in range(3):
            try:
                data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen",
                            params={"bevoegdGezag": code, "page": page, "size": 200})
                break
            except Exception as e:
                wacht = 2 ** poging
                console.print(f"    [yellow]retry {poging+1}/3 voor {code} p{page} "
                              f"({str(e)[:60]}) — wacht {wacht}s[/yellow]")
                time.sleep(wacht)
        if data is None:
            console.print(f"    [red]API fail {code} page {page} na 3 retries — markeer als onbekend[/red]")
            return None
        items = data.get("_embedded", {}).get("regelingen", [])
        for reg in items:
            if reg.get("aangeleverdDoorEen", {}).get("code") != code:
                continue
            ident = reg["identificatie"]
            if ident in out:
                continue
            status_raw = reg.get("regelingstatus") or reg.get("status")
            status = status_raw.get("waarde") if isinstance(status_raw, dict) else status_raw
            out[ident] = {
                "work": ident,
                "type": reg.get("type", {}).get("waarde", ""),
                "titel": reg.get("officieleTitel", ""),
                "status": status,
            }
        if not data.get("_links", {}).get("next", {}).get("href"):
            break
    return list(out.values())


def lokaal_alles(cur) -> dict[str, list[dict]]:
    cur.execute("""
        SELECT bronhouder, frbr_work, documenttype, opschrift
        FROM p2p.regeling
    """)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in cur.fetchall():
        out[r["bronhouder"]].append({
            "work": r["frbr_work"],
            "type": r["documenttype"],
            "titel": r["opschrift"],
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes", help="comma-gescheiden bronhouder-codes (pv25,ws0653)")
    ap.add_argument("--prefix", help="prefix filter, comma-gescheiden (pv,mn,ws,gm)")
    ap.add_argument("--details", action="store_true",
                    help="lijst ontbrekende/overbodige regelingen per bronhouder")
    ap.add_argument("--min-gap", type=int, default=1,
                    help="alleen bronhouders met >= N afwijkingen tonen (default 1)")
    ap.add_argument("--limit", type=int, help="max aantal bronhouders (debug/sample)")
    args = ap.parse_args()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            lokaal = lokaal_alles(cur)

        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        else:
            codes = sorted(lokaal.keys())
            if args.prefix:
                prefixes = tuple(p.strip() for p in args.prefix.split(",") if p.strip())
                codes = [c for c in codes if c.startswith(prefixes)]

        if args.limit:
            codes = codes[:args.limit]

        console.print(f"[bold]{len(codes)}[/bold] bronhouder(s) te scannen "
                      f"({sum(len(lokaal.get(c, [])) for c in codes)} lokale regelingen)")

        rapport = []
        api_fails: list[str] = []
        for i, code in enumerate(codes, 1):
            dso = dso_voor_bronhouder(code)
            lok = lokaal.get(code, [])
            if dso is None:
                api_fails.append(code)
                console.print(f"  [dim]({i}/{len(codes)})[/dim] {code}: "
                              f"lokaal {len(lok)} ↔ DSO [red]API_FAIL[/red] — overgeslagen")
                continue
            dso_w = {r["work"]: r for r in dso}
            lok_w = {r["work"]: r for r in lok}
            ontbr = [dso_w[w] for w in dso_w if w not in lok_w]
            overb = [lok_w[w] for w in lok_w if w not in dso_w]
            marker = ""
            if len(ontbr) > 0:
                marker = f" [red]+{len(ontbr)} mist[/red]"
            if len(overb) > 0:
                marker += f" [yellow]-{len(overb)} over[/yellow]"
            console.print(f"  [dim]({i}/{len(codes)})[/dim] {code}: "
                          f"lokaal {len(lok)} ↔ DSO {len(dso)}{marker}")
            if (len(ontbr) + len(overb)) >= args.min_gap:
                rapport.append({
                    "code": code, "lokaal": len(lok), "dso": len(dso),
                    "ontbrekend": ontbr, "overbodig": overb,
                })

        if api_fails:
            console.print(f"\n[yellow]⚠ {len(api_fails)} bronhouder(s) konden niet "
                          f"gescand worden:[/yellow] {', '.join(api_fails)}")
            console.print(f"  [dim]Hervraag deze later met --codes {','.join(api_fails)}[/dim]")

        if not rapport:
            console.print(f"\n[green]Geen verschillen >= {args.min_gap}[/green]")
            return

        t = Table(title=f"DSO ↔ lokaal diff ({len(rapport)} bronhouders met afwijking)")
        t.add_column("bronhouder")
        t.add_column("lokaal", justify="right")
        t.add_column("DSO", justify="right")
        t.add_column("mist", justify="right", style="red")
        t.add_column("over", justify="right", style="yellow")
        for d in sorted(rapport, key=lambda x: (-len(x["ontbrekend"]), -len(x["overbodig"]))):
            t.add_row(d["code"], str(d["lokaal"]), str(d["dso"]),
                      str(len(d["ontbrekend"])), str(len(d["overbodig"])))
        console.print(t)

        tot_ontbr = sum(len(d["ontbrekend"]) for d in rapport)
        tot_overb = sum(len(d["overbodig"]) for d in rapport)
        console.print(f"\n[bold]Totaal:[/bold] [red]{tot_ontbr} ontbrekend[/red], "
                      f"[yellow]{tot_overb} overbodig[/yellow] "
                      f"over {len(rapport)} bronhouder(s)")

        if args.details:
            for d in sorted(rapport, key=lambda x: -len(x["ontbrekend"])):
                if not d["ontbrekend"] and not d["overbodig"]:
                    continue
                console.print(f"\n[bold cyan]{d['code']}[/bold cyan] "
                              f"(lokaal {d['lokaal']} / DSO {d['dso']})")
                for r in d["ontbrekend"]:
                    status = f" [{r.get('status')}]" if r.get("status") else ""
                    console.print(f"  [red]MIST[/red] [{r['type']}]{status} "
                                  f"{r['titel'][:90]}")
                    console.print(f"       [dim]{r['work']}[/dim]")
                for r in d["overbodig"]:
                    console.print(f"  [yellow]OVER[/yellow] [{r.get('type')}] "
                                  f"{r['titel'][:90]}")
                    console.print(f"       [dim]{r['work']}[/dim]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
