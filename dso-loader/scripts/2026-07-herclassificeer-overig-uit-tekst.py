"""Backfill: haal besluiten uit het `overig`-bucket en vul ontbrekende zaaknummers.

Achtergrond
-----------
`classify_type_besluit` kijkt alleen naar de TITEL. Bronhouders die hun uitkomst
niet in de titel zetten ("Besluit Omgevingsvergunning - Eindhovensingel 125 in
Arnhem") belandden voluit in `overig` — en daarmee buiten
`vth.dossier_doorlooptijd`, want die matview kent alleen aanvraag + terminale
besluiten. Gevolg: hele gemeenten (Arnhem, Gouda, Enschede) ontbraken in de
doorlooptijd-cijfers, en 73 gemeenten hadden 1-9 "clusterbare" dossiers — een
meetartefact, geen vergunningrealiteit.

Twee reparaties, beide uit al aanwezige `inhoud_tekst` (geen her-harvest):

  1. type_besluit — tweede trap `classify_type_besluit_uit_tekst()`, uitsluitend
     op rijen die nu 'overig' zijn. De herkomst wordt vastgelegd in
     `type_besluit_bron` ('tekst'), zodat de wijziging auditeerbaar en
     terugdraaibaar is (`--revert`).
  2. zaaknummer_bg — de labelpatronen accepteerden geen punt in het nummer,
     waardoor Rotterdam (OMV.24.31.12345), Arnhem (N26AB.1150) en Huizen
     (Z.467853) volledig gemist werden.

  3. zaaknummer_bg opschonen — `kenmerk[:\\s]+(...)` accepteerde een spatie als
     scheidingsteken, dus "kenmerk waarvan ..." leverde het zaaknummer
     'waarvan' op. 6.676 records dragen zo'n woord-zaaknummer (123 verschillende
     woorden, 'vermelden' over 5 bronhouders). Binnen één bronhouder klonteren
     die records tot één nepdossier. Ze worden geleegd en opnieuw geëxtraheerd.

Gebruik:
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --sample     # leest, schrijft niet
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --apply      # lokale DB
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --push-prod  # uitkomst naar productie
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --revert     # bron='tekst' terug naar 'overig'

Idempotent: --apply en --push-prod mogen herhaald worden.
Draai daarna `REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;`.

LET OP voor productie: gebruik `--push-prod`, NIET `refresh-koop-to-prod.ps1 -Push`.
Die laatste synchroniseert een delta op `datum_publicatie >= watermark`, en de
herclassificatie raakt de hele historie vanaf 2024 — die rijen zouden dus stil
achterblijven. Vereist de tijdelijke Railway TCP-proxy (dashboard-only).
"""
import pathlib
import sys
import re
import argparse
import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.db import get_conn
from src.loaders.koop_vergunning import (
    classify_type_besluit_uit_tekst,
    extract_zaaknummer_bg,
)

T = "vth.vergunningkennisgeving"


def _wegschrijven(conn, kolom: str, paren: list[tuple[str, str]], extra_set: str = ""):
    """Zet `kolom` per koop_id in ÉÉN UPDATE via een TEMP-tabel + COPY.

    Rij-voor-rij updaten haalde ~2.000 rijen/min op deze tabel (random heap-IO
    plus een round-trip per rij) — 25 minuten voor stap 1, en op de Railway-DB
    navenant erger. Set-based met een COPY erin is een kwestie van seconden.
    `paren` is [(waarde, koop_id), ...].
    """
    if not paren:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _bf (koop_id TEXT PRIMARY KEY, waarde TEXT) "
                    "ON COMMIT DROP")
        with cur.copy("COPY _bf (koop_id, waarde) FROM STDIN") as cp:
            for waarde, koop_id in paren:
                cp.write_row((koop_id, waarde))
        cur.execute(
            f"UPDATE {T} t SET {kolom} = b.waarde{extra_set} "
            f"FROM _bf b WHERE t.koop_id = b.koop_id"
        )
        n = cur.rowcount
        conn.commit()
    print(f"   weggeschreven: {n:,} rijen")


NIEUWE_TYPES = ["buiten_behandeling", "vergunningvrij"]


def _zorg_voor_schema(cur):
    """Kolom + verruimde CHECK — idempotent, spiegelt src/ddl.py."""
    cur.execute(f"ALTER TABLE {T} ADD COLUMN IF NOT EXISTS type_besluit_bron TEXT")
    cur.execute(
        f"ALTER TABLE {T} DROP CONSTRAINT IF EXISTS "
        f"vergunningkennisgeving_type_besluit_check"
    )
    toegestaan = [
        "aanvraag", "verleend", "geweigerd", "ontwerp", "van_rechtswege",
        "ingetrokken", "verlenging_beslistermijn", "melding",
        "melding_geaccepteerd", "kennisgeving", "rectificatie", "overig",
    ] + NIEUWE_TYPES
    lijst = ", ".join(f"'{w}'" for w in toegestaan)
    cur.execute(
        f"ALTER TABLE {T} ADD CONSTRAINT vergunningkennisgeving_type_besluit_check "
        f"CHECK (type_besluit IS NULL OR type_besluit IN ({lijst}))"
    )


# ---------------------------------------------------------------- 1. type_besluit
def herclassificeer(conn, apply: bool) -> collections.Counter:
    tel = collections.Counter()
    updates = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT koop_id, inhoud_tekst FROM {T} "
            f"WHERE type_besluit = 'overig' AND inhoud_tekst IS NOT NULL"
        )
        for r in cur:
            nieuw = classify_type_besluit_uit_tekst(r["inhoud_tekst"])
            if nieuw:
                tel[nieuw] += 1
                updates.append((nieuw, r["koop_id"]))
            else:
                tel["(onbeslist)"] += 1
    if apply:
        _wegschrijven(conn, "type_besluit", updates,
                      extra_set=", type_besluit_bron = 'tekst'")
    return tel


# ------------------------------------------------- 2a. woord-zaaknummers opschonen
def schoon_zaaknummers(conn, apply: bool) -> int:
    """Leeg zaaknummers zonder cijfer ('waarvan', 'vermelden', 'besluit').

    Die zijn afkomstig van het oude `kenmerk[:\\s]+`-patroon dat een spatie als
    scheidingsteken accepteerde. Ze klonteren losse records tot nepdossiers.
    Na het legen pikt vul_zaaknummers() het echte nummer alsnog op.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM {T} "
            f"WHERE zaaknummer_bg IS NOT NULL AND zaaknummer_bg !~ '[0-9]'"
        )
        n = cur.fetchone()["n"]
        if apply and n:
            cur.execute(
                f"UPDATE {T} SET zaaknummer_bg = NULL "
                f"WHERE zaaknummer_bg IS NOT NULL AND zaaknummer_bg !~ '[0-9]'"
            )
            conn.commit()
    return n


# ---------------------------------------------------------------- 2b. zaaknummer_bg
def vul_zaaknummers(conn, apply: bool) -> int:
    updates = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT koop_id, inhoud_tekst FROM {T} "
            f"WHERE zaaknummer_bg IS NULL AND inhoud_tekst IS NOT NULL"
        )
        for r in cur:
            zn = extract_zaaknummer_bg(r["inhoud_tekst"])
            if zn:
                updates.append((zn, r["koop_id"]))
    if apply:
        _wegschrijven(conn, "zaaknummer_bg", updates)
    return len(updates)


# ---------------------------------------------------------------- 3. naar productie
def _prod_conn(dsn: str):
    """Aparte connectie naar prod; parallelisme uit (Railway /dev/shm)."""
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    with c.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET max_parallel_maintenance_workers = 0")
    c.commit()
    return c


def _masker(dsn: str) -> str:
    return re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", dsn)


def push_naar_prod(lokaal, dsn: str, ja: bool):
    """Duw de uitkomst van de backfill naar prod — zonder inhoud_tekst.

    Waarom niet gewoon `refresh-koop-to-prod.ps1 -Push`: die synchroniseert een
    DELTA op `datum_publicatie >= watermark` (prod-max minus overlap). De
    herclassificatie raakt juist de hele historie vanaf 2024, dus die rijen
    zouden nooit meegaan. En waarom niet dit script rechtstreeks tegen prod:
    dan moet 120k+253k keer `inhoud_tekst` over de TCP-proxy (honderden MB's)
    terwijl het rekenwerk lokaal al gedaan is. We sturen dus alleen de
    uitkomst: ~90k korte tupels, enkele MB's.

    Idempotent en defensief: type_besluit wordt op prod alléén gezet waar de rij
    daar nog 'overig' is, en zaaknummer_bg alléén waar prod nog leeg is. Een
    nieuwere waarde op prod wordt dus nooit overschreven.
    """
    print(f"Doelwit: PRODUCTIE → {_masker(dsn)}")
    if not ja:
        try:
            if input("\n⚠  Je gaat DIRECT tegen PRODUCTIE schrijven. "
                     "Typ exact 'PROD' om door te gaan: ").strip() != "PROD":
                raise SystemExit("Afgebroken door gebruiker.")
        except EOFError:
            raise SystemExit("Non-interactief zonder --yes; afgebroken.")

    with lokaal.cursor() as cur:
        cur.execute(f"SELECT koop_id, type_besluit FROM {T} WHERE type_besluit_bron = 'tekst'")
        types = [(r["type_besluit"], r["koop_id"]) for r in cur.fetchall()]
        cur.execute(f"SELECT koop_id, zaaknummer_bg FROM {T} WHERE zaaknummer_bg IS NOT NULL")
        zaaknrs = [(r["zaaknummer_bg"], r["koop_id"]) for r in cur.fetchall()]
    # Leestransactie sluiten vóór we naar het doelwit schrijven: hij houdt
    # ACCESS SHARE op de tabel, en dat blokkeert de ALTER TABLE hieronder zodra
    # doelwit en bron dezelfde database zijn (bv. bij een test met --dsn lokaal).
    lokaal.commit()
    print(f"  lokaal: {len(types):,} herclassificaties, {len(zaaknrs):,} zaaknummers")

    prod = _prod_conn(dsn)
    try:
        with prod.cursor() as cur:
            _zorg_voor_schema(cur)
        prod.commit()
        print("  prod-schema bij (kolom + verruimde CHECK)")

        # Woord-zaaknummers native opruimen — geen transfer nodig.
        with prod.cursor() as cur:
            cur.execute(f"UPDATE {T} SET zaaknummer_bg = NULL "
                        f"WHERE zaaknummer_bg IS NOT NULL AND zaaknummer_bg !~ '[0-9]'")
            print(f"  woord-zaaknummers geleegd op prod: {cur.rowcount:,}")
        prod.commit()

        _kopieer_en_update(
            prod, types,
            f"UPDATE {T} t SET type_besluit = b.waarde, type_besluit_bron = 'tekst' "
            f"FROM _bf b WHERE t.koop_id = b.koop_id AND t.type_besluit = 'overig'",
            "type_besluit")
        _kopieer_en_update(
            prod, zaaknrs,
            f"UPDATE {T} t SET zaaknummer_bg = b.waarde "
            f"FROM _bf b WHERE t.koop_id = b.koop_id AND t.zaaknummer_bg IS NULL",
            "zaaknummer_bg")
    finally:
        prod.close()
    print("\nKlaar. Draai nu op prod:")
    print("  REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;")


def _kopieer_en_update(conn, paren, update_sql: str, label: str):
    if not paren:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _bf (koop_id TEXT PRIMARY KEY, waarde TEXT) "
                    "ON COMMIT DROP")
        with cur.copy("COPY _bf (koop_id, waarde) FROM STDIN") as cp:
            for waarde, koop_id in paren:
                cp.write_row((koop_id, waarde))
        cur.execute(update_sql)
        print(f"  {label} bijgewerkt op prod: {cur.rowcount:,} rijen "
              f"(van {len(paren):,} aangeboden)")
        conn.commit()


def revert(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {T} SET type_besluit = 'overig', type_besluit_bron = NULL "
            f"WHERE type_besluit_bron = 'tekst'"
        )
        n = cur.rowcount
        conn.commit()
    print(f"Teruggedraaid: {n:,} rijen terug naar 'overig'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="schrijf de wijzigingen weg")
    ap.add_argument("--sample", action="store_true", help="alleen tellen, niets schrijven")
    ap.add_argument("--revert", action="store_true", help="draai de tekst-trap terug")
    ap.add_argument("--push-prod", action="store_true",
                    help="duw de lokale uitkomst naar productie (PROD_DB_URL uit .env)")
    ap.add_argument("--dsn", help="expliciete doelwit-DSN i.p.v. PROD_DB_URL")
    ap.add_argument("--yes", action="store_true", help="sla de PROD-typbevestiging over")
    args = ap.parse_args()
    if not (args.apply or args.sample or args.revert or args.push_prod):
        ap.error("kies --sample, --apply, --revert of --push-prod")

    with get_conn() as conn:
        if args.push_prod:
            dsn = args.dsn
            if not dsn:
                from dotenv import dotenv_values
                dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
            if not dsn:
                raise SystemExit("PROD_DB_URL ontbreekt in .env en geen --dsn gegeven.")
            push_naar_prod(conn, dsn.strip().strip('"').strip("'"), args.yes)
            return
        if args.revert:
            revert(conn)
            return
        if args.apply:
            with conn.cursor() as cur:
                _zorg_voor_schema(cur)
            conn.commit()

        print("1. type_besluit uit inhoud_tekst (alleen rijen die nu 'overig' zijn)")
        tel = herclassificeer(conn, args.apply)
        totaal = sum(tel.values())
        for k, n in tel.most_common():
            print(f"   {k:24s} {n:>8,} ({100 * n / totaal:5.1f}%)")
        gewonnen = totaal - tel["(onbeslist)"]
        print(f"   -> {gewonnen:,} van {totaal:,} records krijgen een uitkomst")

        print("\n2a. woord-zaaknummers opschonen (geen cijfer erin)")
        vuil = schoon_zaaknummers(conn, args.apply)
        print(f"   -> {vuil:,} geleegd")

        print("\n2b. zaaknummer_bg uit inhoud_tekst (alleen lege)")
        n = vul_zaaknummers(conn, args.apply)
        print(f"   -> {n:,} zaaknummers gevuld")

        if not args.apply:
            print("\n(sample-modus: er is niets weggeschreven)")
        else:
            print("\nKlaar. Draai nu:")
            print("  REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;")


if __name__ == "__main__":
    main()
