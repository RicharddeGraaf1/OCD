"""Markeer regelingen die uit de DSO-lijst verdwenen én aantoonbaar vervallen zijn.

Sluit G-91: de sync is additief en laat een regeling die het DSO niet meer toont
gewoon als vigerend staan. Dit script doet de omgekeerde diff en zet die
regelingen op `inactief=true, reden_inactief='ingetrokken'`.

Werkwijze (goedkoop: ~10 lijst-calls + 1 call per kandidaat):

  1. Haal de **volledige** `/regelingen`-lijst op (Presenteren v8; de lijst is
     een tijdreis naar vandaag, dus wat er niet in staat geldt vandaag niet).
  2. Onze vigerende works die daar niet in staan = **kandidaten**.
  3. Ververs `p2p.regeling_voorkomen` voor die kandidaten via de bestaande
     voorkomens-loader. Het voorkomens-endpoint blijft antwoorden voor
     regelingen die uit de lijst zijn verdwenen — dat is precies het bewijs.
  4. Classificeer op de voorkomens-tijdlijn:

       vervallen  : geen enkel voorkomen is vandaag geldig én er is geen
                    voorkomen met een begindatum in de toekomst
                    -> markeren als 'ingetrokken'
       toekomstig : er is een voorkomen dat later begint (nog niet geldig)
                    -> NIET markeren, alleen melden
       nog-geldig : er is wél een vandaag geldig voorkomen terwijl het work
                    uit de lijst ontbreekt -> NIET markeren, dit is een
                    signaal (lijst-hiaat of scope-filter), geen intrekking
       onbekend   : geen voorkomens gevonden -> NIET markeren

Alleen de eerste categorie wordt aangeraakt, en alleen die. Een regeling
verdwijnt hiermee uit de retrieval (`AND NOT r.inactief`); fysiek opruimen is
een aparte, bewuste stap (`prune_verouderde_versies.py --reden ingetrokken`).

Waarom niet op de 404 van `/regelingen/{work}` vertrouwen: die geeft ook 404
voor works die de API niet los teruggeeft (programma's, tijdelijkdelen) — zie
de fallback in `markeer_verouderde_expressies.py`. De voorkomens-tijdlijn is
het enige harde criterium.

Achtergrond: vault `gaps.md` G-91, `analysis/Objectversie-bewuste synchronisatie
via registratiegegevens.md` §7.4, en de keuze van 2026-09-07 (OCD bevat alleen
actuele data).

Gebruik:
    python scripts/markeer_vervallen_regelingen.py                 # droogloop
    python scripts/markeer_vervallen_regelingen.py --uitvoeren     # markeren
    python scripts/markeer_vervallen_regelingen.py --json rap.json
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# Zonder dit klapt elke pijl/en-dash zodra de uitvoer naar een bestand gaat:
# Windows kiest dan cp1252. Zelfde regel staat in prune_verouderde_versies.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.table import Table

from src.db import get_conn
from src.loaders.api_loader import find_regelingen_delta
from src.loaders.voorkomens_loader import load_voorkomens

console = Console()

# Classificatie per kandidaat-work op basis van p2p.regeling_voorkomen.
CLASSIFICATIE_SQL = """
SELECT r.frbr_expression,
       r.frbr_work,
       r.documenttype,
       r.bronhouder,
       v.n_voorkomens,
       v.geldig_vandaag,
       v.toekomstig,
       v.laatste_eind,
       CASE
         WHEN v.n_voorkomens IS NULL OR v.n_voorkomens = 0 THEN 'onbekend'
         WHEN v.geldig_vandaag > 0                         THEN 'nog-geldig'
         WHEN v.toekomstig     > 0                         THEN 'toekomstig'
         WHEN v.laatste_eind IS NOT NULL
              AND v.laatste_eind <= CURRENT_DATE           THEN 'vervallen'
         ELSE 'onbekend'
       END AS oordeel
  FROM p2p.regeling r
  LEFT JOIN LATERAL (
        SELECT count(*)                                            AS n_voorkomens,
               count(*) FILTER (WHERE geldig @> CURRENT_DATE)       AS geldig_vandaag,
               count(*) FILTER (WHERE begin_geldigheid > CURRENT_DATE) AS toekomstig,
               max(eind_geldigheid)                                 AS laatste_eind
          FROM p2p.regeling_voorkomen vv
         WHERE vv.frbr_work = r.frbr_work
  ) v ON TRUE
 WHERE r.frbr_work = ANY(%s)
   AND NOT r.inactief
 ORDER BY r.bronhouder, r.frbr_work
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uitvoeren", action="store_true",
                    help="markeer daadwerkelijk (zonder deze vlag: droogloop)")
    ap.add_argument("--json", help="schrijf het volledige oordeel naar dit pad")
    ap.add_argument("--sla-voorkomens-over", action="store_true",
                    help="p2p.regeling_voorkomen niet verversen (gebruikt de "
                         "bestaande rijen; alleen voor herhaald draaien)")
    ap.add_argument("--max", type=int, default=100, metavar="N",
                    help="weiger te markeren boven N kandidaten (default 100); "
                         "een plotselinge piek betekent eerder een half opgehaalde "
                         "lijst dan honderden intrekkingen")
    ap.add_argument("--forceer", action="store_true",
                    help="markeer ook boven de --max-drempel")
    args = ap.parse_args()

    # ── 1. De volledige DSO-lijst ──
    console.print("[bold]Stap 1[/bold] — volledige /regelingen-lijst ophalen")
    dso = find_regelingen_delta(None, bronhouder_codes=None)
    dso_works = {r["identificatie"] for r in dso}
    if len(dso_works) < 500:
        raise SystemExit(f"Slechts {len(dso_works)} works uit de DSO-lijst — dat is "
                         "te weinig om een verdwijning op te baseren. Afgebroken.")

    # ── 2. Onze vigerende works die er niet in staan ──
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT frbr_work FROM p2p.regeling WHERE NOT inactief")
            onze_works = {r["frbr_work"] for r in cur.fetchall()}
    finally:
        conn.close()

    kandidaten = sorted(onze_works - dso_works)
    console.print(f"\n[bold]Stap 2[/bold] — {len(onze_works)} vigerende works lokaal, "
                  f"{len(dso_works)} in de DSO-lijst → "
                  f"[yellow]{len(kandidaten)} kandidaat-verdwenen[/yellow]")
    if not kandidaten:
        console.print("[green]Niets te doen.[/green]")
        return

    # ── 3. Voorkomens verversen (het bewijs) ──
    if args.sla_voorkomens_over:
        console.print("\n[bold]Stap 3[/bold] — overgeslagen (--sla-voorkomens-over)")
    else:
        console.print(f"\n[bold]Stap 3[/bold] — voorkomens ophalen voor {len(kandidaten)} works")
        load_voorkomens(works=kandidaten)

    # ── 4. Classificeren ──
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CLASSIFICATIE_SQL, (kandidaten,))
            rijen = [dict(r) for r in cur.fetchall()]

        per_oordeel: dict[str, list] = {}
        for r in rijen:
            per_oordeel.setdefault(r["oordeel"], []).append(r)

        console.print(f"\n[bold]Stap 4[/bold] — oordeel over {len(rijen)} vigerende expressies")
        t = Table()
        t.add_column("oordeel"); t.add_column("n", justify="right"); t.add_column("actie")
        acties = {"vervallen": "markeren als 'ingetrokken'",
                  "toekomstig": "niets — versie begint later",
                  "nog-geldig": "niets — ONDERZOEKEN (lijst-hiaat?)",
                  "onbekend": "niets — geen voorkomens"}
        for oordeel in ("vervallen", "toekomstig", "nog-geldig", "onbekend"):
            if oordeel in per_oordeel:
                t.add_row(oordeel, str(len(per_oordeel[oordeel])), acties[oordeel])
        console.print(t)

        for oordeel in ("vervallen", "toekomstig", "nog-geldig", "onbekend"):
            for r in per_oordeel.get(oordeel, []):
                kleur = "yellow" if oordeel == "vervallen" else "dim"
                console.print(f"  [{kleur}]{oordeel:11s}[/{kleur}] {r['bronhouder']:10s} "
                              f"{r['documenttype'][:32]:32s} eind={r['laatste_eind']} "
                              f"{r['frbr_work']}")

        te_markeren = per_oordeel.get("vervallen", [])

        # ── 5. Markeren ──
        if not te_markeren:
            console.print("\n[green]Geen vervallen regelingen te markeren.[/green]")
        elif not args.uitvoeren:
            console.print(f"\n[bold yellow]DROOGLOOP[/bold yellow] — {len(te_markeren)} "
                          f"expressies zouden op inactief='ingetrokken' gaan. "
                          f"Draai met --uitvoeren om het te doen.")
        elif len(te_markeren) > args.max and not args.forceer:
            # Een half opgehaalde lijst is aannemelijker dan honderden
            # intrekkingen op één dag. De voorkomens-check vangt dat al af (die
            # regelingen komen als 'nog-geldig' uit), maar deze klep staat er
            # voor het geval de DSO ooit óók de voorkomens leeg teruggeeft.
            raise SystemExit(
                f"\n{len(te_markeren)} kandidaten boven de drempel van {args.max}. "
                f"Dat is verdacht: controleer eerst of de /regelingen-lijst "
                f"volledig is opgehaald. Bewust toch doorzetten: --forceer.")
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE p2p.regeling
                          SET inactief       = TRUE,
                              reden_inactief = 'ingetrokken',
                              datum_inactief = now()
                        WHERE frbr_expression = ANY(%s)
                          AND NOT inactief""",
                    ([r["frbr_expression"] for r in te_markeren],),
                )
                n = cur.rowcount
            conn.commit()
            console.print(f"\n[bold green]{n} expressies gemarkeerd als "
                          f"'ingetrokken'.[/bold green]")
            console.print("[dim]Terug te draaien met: UPDATE p2p.regeling SET "
                          "inactief=false, reden_inactief=NULL, datum_inactief=NULL "
                          "WHERE reden_inactief='ingetrokken' AND datum_inactief::date "
                          "= CURRENT_DATE;[/dim]")
            console.print("[dim]Prod volgt bij de eerstvolgende p2p-replicatie "
                          "(die werkt bestaande rijen bij).[/dim]")

        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(
                json.dumps({"peildatum": dt.date.today().isoformat(),
                            "dso_works": len(dso_works),
                            "onze_works": len(onze_works),
                            "kandidaten": kandidaten,
                            "uitgevoerd": bool(args.uitvoeren),
                            "oordelen": rijen}, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8")
            console.print(f"\nrapport: {args.json}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
