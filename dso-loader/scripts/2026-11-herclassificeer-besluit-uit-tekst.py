"""Backfill: besluiten die als `aanvraag` of `kennisgeving` te boek staan.

Achtergrond
-----------
`classify_type_besluit` kijkt alleen naar de titel. "Kennisgeving besluit op
aanvraag het renoveren van de woning" bevat geen uitkomstwoord en valt door naar
de regel `aanvraag`. Gemeten 2026-11-02 op de lokale DB: 43.739 besluiten staan
zo als `aanvraag`, en 14.145 als `kennisgeving`. De uitkomst staat meestal gewoon
in de tekst ("De vergunning is verleend"), maar de tweede trap draaide alleen op
`overig`.

Twee gevolgen:
  - vth.dossier_doorlooptijd kent ze niet als besluit. Van de 53.811 zaken met zo'n
    bericht hadden er 262 nog een ander terminaal besluit; de rest ontbreekt in het
    publieke doorlooptijd-dashboard (nu 171.187 dossiers).
  - De viewer kan alleen "Besloten" zeggen waar de tekst "Verleend" zegt.

Alleen rijen waar de tekst een uitkomst noemt veranderen. De rest blijft staan: de
API herkent die zelf als besluit uit de titel (ocd-api/vergunning_zaken.py).

De herkomst komt in `type_besluit_bron` ('tekst:aanvraag' of 'tekst:kennisgeving'),
dus --revert zet exact de oude waarde terug.

Gebruik:
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py            # meet, schrijft niet
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --apply    # lokale DB
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --revert   # lokale DB terug

Draai na --apply: REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;

LET OP. Dit verandert publieke cijfers op omgevingsvergunningenregister.nl: facetten
en doorlooptijden. En de reguliere sync neemt deze rijen niet mee, die synchroniseert
een delta op datum_publicatie. Voor productie is dus een eigen stap nodig, zie
push_naar_prod in 2026-07-herclassificeer-overig-uit-tekst.py; die selecteert op
bron = 'tekst' en moet dan op deze bronwaarden worden aangepast.
"""
import argparse
import collections
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.db import get_conn
from src.loaders.koop_vergunning import classify_type_besluit_uit_tekst, titel_meldt_besluit

T = "vth.vergunningkennisgeving"
BRONNEN = ("tekst:aanvraag", "tekst:kennisgeving")


def meet(conn) -> tuple[collections.Counter, list[tuple[str, str, str]]]:
    tel: collections.Counter = collections.Counter()
    updates = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT koop_id, titel, type_besluit, inhoud_tekst FROM {T} "
            "WHERE type_besluit IN ('aanvraag', 'kennisgeving') AND titel ~* 'besluit' "
            "AND inhoud_tekst IS NOT NULL AND type_besluit_bron IS NULL"
        )
        for r in cur:
            if not titel_meldt_besluit(r["titel"]):
                continue
            nieuw = classify_type_besluit_uit_tekst(r["inhoud_tekst"])
            # "aanvraag" uit de tekst zegt niets nieuws over een besluit-titel.
            if nieuw and nieuw != "aanvraag":
                tel[(r["type_besluit"], nieuw)] += 1
                updates.append((nieuw, f"tekst:{r['type_besluit']}", r["koop_id"]))
            else:
                tel[(r["type_besluit"], "(onbeslist)")] += 1
    return tel, updates


def schrijf(conn, updates: list[tuple[str, str, str]]) -> None:
    """Set-based via een TEMP-tabel: rij voor rij haalt op deze tabel ~2.000 rijen/min."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _bf (koop_id TEXT PRIMARY KEY, waarde TEXT, bron TEXT) ON COMMIT DROP"
        )
        with cur.copy("COPY _bf (waarde, bron, koop_id) FROM STDIN") as cp:
            for rij in updates:
                cp.write_row(rij)
        cur.execute(
            f"UPDATE {T} t SET type_besluit = b.waarde, type_besluit_bron = b.bron "
            "FROM _bf b WHERE t.koop_id = b.koop_id"
        )
        print(f"weggeschreven: {cur.rowcount:,} rijen")
    conn.commit()


def revert(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {T} SET type_besluit = split_part(type_besluit_bron, ':', 2), "
            "type_besluit_bron = NULL WHERE type_besluit_bron = ANY(%s)",
            [list(BRONNEN)],
        )
        print(f"teruggezet: {cur.rowcount:,} rijen")
    conn.commit()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="schrijf naar de lokale DB (default: alleen meten)")
    p.add_argument("--revert", action="store_true", help="zet deze herclassificatie terug")
    args = p.parse_args()

    with get_conn() as conn:
        if args.revert:
            revert(conn)
            return
        tel, updates = meet(conn)
        totaal = sum(tel.values())
        for (oud, nieuw), n in sorted(tel.items(), key=lambda kv: (kv[0][0], -kv[1])):
            print(f"  {oud:14s} -> {nieuw:22s} {n:>8,} ({100 * n / totaal:5.1f}%)")
        print(f"  {len(updates):,} van {totaal:,} besluiten krijgen een uitkomst")
        if args.apply:
            schrijf(conn, updates)
            print("Ververs nu vth.dossier_doorlooptijd.")
        else:
            print("Niets geschreven. Gebruik --apply om de lokale DB bij te werken.")


if __name__ == "__main__":
    main()
