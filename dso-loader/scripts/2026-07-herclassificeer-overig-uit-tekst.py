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
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --sample   # leest, schrijft niet
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --apply
  python scripts/2026-07-herclassificeer-overig-uit-tekst.py --revert   # alles met bron='tekst' terug naar 'overig'

Idempotent: --apply mag herhaald worden.
Draai daarna `REFRESH MATERIALIZED VIEW CONCURRENTLY vth.dossier_doorlooptijd;`.
"""
import sys
import argparse
import collections

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, ".")
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
    args = ap.parse_args()
    if not (args.apply or args.sample or args.revert):
        ap.error("kies --sample, --apply of --revert")

    with get_conn() as conn:
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
