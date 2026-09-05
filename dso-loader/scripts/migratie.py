#!/usr/bin/env python
"""Pas een SQL-migratie toe en leg vast dát hij is toegepast — per database.

Waarom dit bestaat
------------------
Er zijn 75 bestanden in `scripts/*.sql` en nergens stond welke er op welke
database waren toegepast. Dat is niet theoretisch misgegaan:

- **2026-09-04**: prod miste alle drie de indexen uit
  `2026-09-add-generalisatie-prefix-index.sql`. Zonder die `text_pattern_ops`-index
  kost een herbouw per bronhouder ~25 s vaste voet, óók bij 24 rijen. Niemand had
  het gemerkt; het viel op omdat ik er toevallig naar keek.
- **2026-09-05**: prod miste `p2p.tekstdeel.regeling_expression`. Dát werd wél
  gevangen — door de kolomdrift-controle in `repliceer_p2p_naar_prod.py` — maar
  dat is toeval en geen systeem: die controle kijkt alleen naar de 28 tabellen
  die hij kopieert.

Runbook §3a zegt: wie een backfill draait, levert de overzetstap mee. Dit is
diezelfde afspraak toegepast op **DDL**.

Wat dit wél en niet doet
------------------------
**Wel**: vanaf nu bijhouden wat er is toegepast, met een checksum, zodat
"welke migraties mist prod?" een query is in plaats van een herinnering.

**Niet**: doen alsof het het verleden kent. De 75 bestaande migraties worden
*niet* met terugwerkende kracht als toegepast gemarkeerd — dat zou een gok zijn,
en een ledger die liegt is erger dan geen ledger. Ze staan als "onbekend" en
worden geregistreerd zodra iemand ze bewust toepast of bevestigt met
`--markeer-toegepast`.

Gebruik
-------
    python scripts/migratie.py --stand                        # wat weet elke kant?
    python scripts/migratie.py --toepassen <bestand.sql> --target prod
    python scripts/migratie.py --markeer-toegepast <bestand.sql> --target prod
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402

SCRIPTS = ROOT / "scripts"

DDL = """
CREATE SCHEMA IF NOT EXISTS core;
CREATE TABLE IF NOT EXISTS core.migratie (
    bestand       TEXT PRIMARY KEY,
    checksum      TEXT NOT NULL,
    toegepast_op  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 'toegepast' = door dit script uitgevoerd; 'gemarkeerd' = met de hand
    -- bevestigd zonder uitvoering (voor de voorraad van vóór 2026-09-05).
    herkomst      TEXT NOT NULL DEFAULT 'toegepast'
);
COMMENT ON TABLE core.migratie IS
    'Welke scripts/*.sql zijn op DEZE database toegepast. Zie vault-leerpunt 5: '
    'prod miste tot 2026-09-05 stil migraties en niets meldde dat.';
"""


def lokale_dsn() -> str:
    return (f"host={os.getenv('DB_HOST')} port={os.getenv('DB_PORT')} "
            f"dbname={os.getenv('DB_NAME')} user={os.getenv('DB_USER')} "
            f"password={os.getenv('DB_PASSWORD')}")


def dsn_voor(target: str) -> str:
    if target == "prod":
        d = os.getenv("PROD_DB_URL")
        if not d:
            raise SystemExit("PROD_DB_URL ontbreekt in .env")
        return d
    return lokale_dsn()


def checksum(pad: pathlib.Path) -> str:
    return hashlib.md5(pad.read_bytes()).hexdigest()


def geregistreerd(dsn: str) -> dict[str, tuple[str, str]]:
    with psycopg.connect(dsn, connect_timeout=20) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT bestand, checksum, herkomst FROM core.migratie")
            rijen = cur.fetchall()
    uit = {}
    for r in rijen:
        b, ck, hk = (r["bestand"], r["checksum"], r["herkomst"]) if isinstance(r, dict) else r
        uit[b] = (ck, hk)
    return uit


def registreer(dsn: str, naam: str, ck: str, herkomst: str) -> None:
    with psycopg.connect(dsn, connect_timeout=20) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""INSERT INTO core.migratie (bestand, checksum, herkomst)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (bestand) DO UPDATE
                             SET checksum = EXCLUDED.checksum,
                                 toegepast_op = now(),
                                 herkomst = EXCLUDED.herkomst""",
                        (naam, ck, herkomst))


def toon_stand() -> int:
    alle = sorted(p.name for p in SCRIPTS.glob("*.sql"))
    lok = geregistreerd(lokale_dsn())
    prod_dsn = os.getenv("PROD_DB_URL")
    prod = geregistreerd(prod_dsn) if prod_dsn else {}

    mist_prod = [n for n in alle if n in lok and n not in prod]
    mist_lok = [n for n in alle if n in prod and n not in lok]
    drift = [n for n in alle if n in lok and n in prod and lok[n][0] != prod[n][0]]
    onbekend = [n for n in alle if n not in lok and n not in prod]

    print(f"{len(alle)} migratiebestanden in scripts/")
    print(f"  geregistreerd lokaal : {len(lok)}")
    print(f"  geregistreerd prod   : {len(prod)}" if prod_dsn else "  prod: niet bevraagd")
    print(f"  nergens geregistreerd: {len(onbekend)}  "
          f"(voorraad van vóór het ledger — status onbekend, niet 'ontbreekt')")
    if mist_prod:
        print(f"\nPROD MIST {len(mist_prod)}:")
        for n in mist_prod:
            print(f"  {n}")
    if mist_lok:
        print(f"\nLOKAAL MIST {len(mist_lok)}:")
        for n in mist_lok:
            print(f"  {n}")
    if drift:
        print(f"\nCHECKSUM VERSCHILT {len(drift)} — hetzelfde bestand, andere inhoud "
              f"toegepast:")
        for n in drift:
            print(f"  {n}")
    if not (mist_prod or mist_lok or drift):
        print("\ngeen verschil tussen de geregistreerde migraties")
    return 1 if (mist_prod or mist_lok or drift) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stand", action="store_true", help="vergelijk lokaal en prod")
    ap.add_argument("--toepassen", metavar="BESTAND", help="voer uit en registreer")
    ap.add_argument("--markeer-toegepast", metavar="BESTAND",
                    help="alleen registreren, niet uitvoeren (voor bestaande voorraad)")
    ap.add_argument("--target", choices=["local", "prod"], default="local")
    a = ap.parse_args()

    if a.stand:
        return toon_stand()

    naam = a.toepassen or a.markeer_toegepast
    if not naam:
        ap.print_help()
        return 2
    pad = SCRIPTS / pathlib.Path(naam).name
    if not pad.exists():
        print(f"niet gevonden: {pad}", file=sys.stderr)
        return 2

    dsn = dsn_voor(a.target)
    ck = checksum(pad)

    if a.markeer_toegepast:
        registreer(dsn, pad.name, ck, "gemarkeerd")
        print(f"gemarkeerd als toegepast op {a.target}: {pad.name}")
        return 0

    with psycopg.connect(dsn, connect_timeout=20) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = '90min'")
            cur.execute(pad.read_text(encoding="utf-8"))
    registreer(dsn, pad.name, ck, "toegepast")
    print(f"toegepast op {a.target} en geregistreerd: {pad.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
