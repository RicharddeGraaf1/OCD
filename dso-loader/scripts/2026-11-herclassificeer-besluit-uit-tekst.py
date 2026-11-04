"""Backfill: besluiten die als `aanvraag` of `kennisgeving` te boek staan.

Achtergrond
-----------
`classify_type_besluit` kijkt alleen naar de titel. "Kennisgeving besluit op
aanvraag het renoveren van de woning" bevat geen uitkomstwoord en valt door naar
de regel `aanvraag`. Gemeten 2026-11-02 op de lokale DB: 43.701 besluiten met tekst
staan zo als `aanvraag`, en 14.147 als `kennisgeving`. De uitkomst staat meestal gewoon
in de tekst ("De vergunning is verleend"), maar de tweede trap draaide alleen op
`overig`.

Twee gevolgen:
  - vth.dossier_doorlooptijd kent ze niet als besluit. Van de 53.811 zaken met zo'n
    bericht hadden er 262 nog een ander terminaal besluit; de rest ontbreekt in het
    publieke doorlooptijd-dashboard.
  - De viewer kan alleen "Besloten" zeggen waar de tekst "Verleend" zegt.

Alleen rijen waar de tekst een uitkomst noemt veranderen. De rest blijft staan: de
API herkent die zelf als besluit uit de titel (ocd-api/vergunning_zaken.py).

De herkomst komt in `type_besluit_bron` ('tekst:aanvraag' of 'tekst:kennisgeving'),
dus --revert zet exact de oude waarde terug.

Gebruik:
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py            # meet, schrijft niet
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --apply    # lokale DB
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --revert   # lokale DB terug
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --push-prod        # droogloop op prod
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --push-prod --ja   # uitkomst naar prod
  python scripts/2026-11-herclassificeer-besluit-uit-tekst.py --revert-prod --ja # prod terug

Draai na --apply: REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;
--push-prod doet dat zelf op prod, samen met VACUUM en de andere matviews. Hij vraagt
de tunnel uit prod_tunnel.sh en PROD_DB_URL.

LET OP. Dit verandert publieke cijfers op omgevingsvergunningenregister.nl: facetten
en doorlooptijden. En de reguliere sync neemt deze rijen niet mee, die synchroniseert
een delta op datum_publicatie. Daarom --push-prod: die stuurt alleen de uitkomst, geen
inhoud_tekst, en schrijft alleen waar prod nog de oude stand heeft.
"""
import argparse
import collections
import os
import pathlib
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import psycopg  # noqa: E402

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


def _prod_conn() -> psycopg.Connection:
    dsn = (os.environ.get("PROD_DB_URL") or "").strip().strip('"').strip("'")
    if not dsn:
        raise SystemExit("PROD_DB_URL ontbreekt.")
    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        # Parallelle workers lopen op de krappe /dev/shm stuk.
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET max_parallel_maintenance_workers = 0")
        cur.execute("SET statement_timeout = 0")
    conn.commit()
    return conn


def _na_de_prod_stap() -> None:
    """Zelfde afsluiting als fase 3 van refresh-koop-to-prod.ps1."""
    with _prod_conn() as prod:
        prod.autocommit = True
        with prod.cursor() as cur:
            for sql in (
                f"VACUUM (ANALYZE, PARALLEL 0) {T}",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY vth.vergunning_stats",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY vth.vergunning_stats_type_besluit",
            ):
                t = time.time()
                cur.execute(sql)
                print(f"  {time.time() - t:6.0f}s  {sql}", flush=True)


def push_naar_prod(lokaal, ja: bool) -> None:
    """Zet de lokale uitkomst door naar prod. Zonder --ja rolt de transactie terug."""
    with lokaal.cursor() as cur:
        cur.execute(
            f"SELECT koop_id, type_besluit, type_besluit_bron FROM {T} "
            "WHERE type_besluit_bron = ANY(%s)",
            [list(BRONNEN)],
        )
        rijen = [(r["koop_id"], r["type_besluit"], r["type_besluit_bron"]) for r in cur.fetchall()]
    lokaal.commit()
    print(f"lokaal: {len(rijen):,} herclassificaties")

    with _prod_conn() as prod, prod.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _bf (koop_id TEXT PRIMARY KEY, nieuw TEXT, bron TEXT, oud TEXT) ON COMMIT DROP"
        )
        with cur.copy("COPY _bf (koop_id, nieuw, bron, oud) FROM STDIN") as cp:
            for koop_id, nieuw, bron in rijen:
                cp.write_row((koop_id, nieuw, bron, bron.split(":", 1)[1]))
        cur.execute(
            f"""SELECT CASE WHEN t.koop_id IS NULL THEN 'niet op prod'
                            WHEN t.type_besluit = b.oud AND t.type_besluit_bron IS NULL THEN 'te wijzigen'
                            WHEN t.type_besluit = b.nieuw AND t.type_besluit_bron = b.bron THEN 'al gelijk'
                            ELSE 'andere stand op prod, blijft staan' END AS stand, count(*)
                FROM _bf b LEFT JOIN {T} t USING (koop_id) GROUP BY 1 ORDER BY 2 DESC"""
        )
        for stand, n in cur.fetchall():
            print(f"  {stand:36s} {n:>8,}")
        cur.execute(
            f"UPDATE {T} t SET type_besluit = b.nieuw, type_besluit_bron = b.bron FROM _bf b "
            "WHERE t.koop_id = b.koop_id AND t.type_besluit = b.oud AND t.type_besluit_bron IS NULL"
        )
        print(f"UPDATE raakt {cur.rowcount:,} rijen")
        if not ja:
            prod.rollback()
            print("Droogloop: teruggerold, prod is ongewijzigd. Gebruik --ja om te schrijven.")
            return
        prod.commit()
    _na_de_prod_stap()
    print("Klaar.")


def revert_prod(ja: bool) -> None:
    with _prod_conn() as prod, prod.cursor() as cur:
        cur.execute(
            f"UPDATE {T} SET type_besluit = split_part(type_besluit_bron, ':', 2), "
            "type_besluit_bron = NULL WHERE type_besluit_bron = ANY(%s)",
            [list(BRONNEN)],
        )
        print(f"terug te zetten op prod: {cur.rowcount:,} rijen")
        if not ja:
            prod.rollback()
            print("Droogloop: teruggerold. Gebruik --ja om te schrijven.")
            return
        prod.commit()
    _na_de_prod_stap()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="schrijf naar de lokale DB (default: alleen meten)")
    p.add_argument("--revert", action="store_true", help="zet deze herclassificatie terug")
    p.add_argument("--push-prod", action="store_true", help="lokale uitkomst naar prod (droogloop zonder --ja)")
    p.add_argument("--revert-prod", action="store_true", help="zet prod terug (droogloop zonder --ja)")
    p.add_argument("--ja", action="store_true", help="schrijf echt naar prod")
    args = p.parse_args()

    if args.revert_prod:
        revert_prod(args.ja)
        return
    with get_conn() as conn:
        if args.push_prod:
            push_naar_prod(conn, args.ja)
            return
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
