#!/usr/bin/env python
"""Backfill van het tekstdeel-pad naar prod: locatie → gebiedsaanwijzing → tekstdeel → junctions.

Waarom dit los staat van `repliceer_p2p_naar_prod.py`
-----------------------------------------------------
Die replicatie kopieert alleen de rijen van de expressies die *deze run* zijn
geladen. Dat is precies goed voor nieuwe data, en precies verkeerd voor een
**backfill**: op 2026-08-09 is met de reparatie van vault-G-124 landelijk over de
bestaande voorraad geschreven — de `hoofdlijnRefs`/`gebiedsaanwijzingRefs` die
`load_divisieannotaties` nooit las — zonder dat er ook maar één expressie opnieuw
werd geladen. De delta zag die rijen dus nooit, en ziet ze ook nooit meer.

Gemeten 2026-08-28, ná de gewone replicatie van die dag:

    tabel                          lokaal     prod   mist
    p2p.locatie                        —        —      38   (alleen deze keten)
    p2p.gebiedsaanwijzing          5.026    4.988      38
    p2p.tekstdeel                 27.468   27.119     349
    p2p.tekstdeel_gebiedsaanwijzing 7.118    6.919     199
    p2p.tekstdeel_hoofdlijn        4.955        0   4.955

`tekstdeel_hoofdlijn` op **nul** is het zwaarste geval: de hele G-124-reparatie is
nooit op productie aangekomen, terwijl niets daarover klaagde. En het gat zit niet
alleen in de junctions — van de 38 ontbrekende gebiedsaanwijzingen ontbrak ook de
**locatie** waaraan ze hangen. Vandaar dat dit script de hele keten loopt en niet
alleen de twee koppeltabellen.

Wat het doet
------------
Per tabel, in FK-volgorde: bepaal welke lokale rijen op prod ontbreken én daar een
geldige ouder hebben, en kopieer die met `COPY … FORMAT TEXT` (niet BINARY: lokaal
is PG 16/PostGIS 3.5, prod PG 17/PostGIS 3.7). Rijen waarvan de ouder op prod
ontbreekt worden **overgeslagen en geteld**, nooit geforceerd — een ontbrekende
ouder is een ander gat en hoort niet stilzwijgend door een backfill te worden
gemaskeerd.

Idempotent: `ON CONFLICT DO NOTHING`, dus een tweede run doet niets. Droogloop is
de default; `--ja` voert uit.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from scripts.repliceer_p2p_naar_prod import identity_kolommen, kolommen  # noqa: E402

# (tabel, pk-kolommen, [(fk-kolom, oudertabel, ouder-pk), …])
# Volgorde is FK-volgorde: ouders eerst.
KETEN = [
    ("p2p.locatie", ["identificatie"], []),
    ("p2p.locatie_basisgeo", ["locatie_id", "basisgeo_id"],
     [("locatie_id", "p2p.locatie", "identificatie")]),
    ("p2p.gebiedsaanwijzing", ["identificatie"],
     [("locatie_id", "p2p.locatie", "identificatie")]),
    ("p2p.tekstdeel", ["identificatie"],
     [("locatie_id", "p2p.locatie", "identificatie")]),
    ("p2p.tekstdeel_gebiedsaanwijzing", ["tekstdeel_id", "gebiedsaanwijzing_id"],
     [("tekstdeel_id", "p2p.tekstdeel", "identificatie"),
      ("gebiedsaanwijzing_id", "p2p.gebiedsaanwijzing", "identificatie")]),
    # `hoofdlijn` stond hier alleen als ouder genoemd en werd zelf nooit
    # gekopieerd -- dezelfde omissie als eerder bij `p2p.kaart`. Gevolg: de 224
    # ontbrekende `tekstdeel_hoofdlijn`-rijen bleven twee ronden lang hangen op
    # "ouder ontbreekt", met 14 hoofdlijnen als enige blokkade. Gemeten tijdens
    # de sync van 2026-09-04. Geen FK's en geen locatie-anker, dus zonder ouders.
    ("p2p.hoofdlijn", ["identificatie"], []),
    ("p2p.tekstdeel_hoofdlijn", ["tekstdeel_id", "hoofdlijn_id"],
     [("tekstdeel_id", "p2p.tekstdeel", "identificatie"),
      ("hoofdlijn_id", "p2p.hoofdlijn", "identificatie")]),

    # Eén tabel verder in dezelfde keten. Een kaartlaag kan aan een
    # gebiedsaanwijzing hangen, en de gebiedsaanwijzingen uit het tekstdeel-pad
    # vielen buiten de replicatiescope -- dus vielen hún kaarten dat ook.
    # Gemeten 2026-08-29 door diff_lokaal_prod.py: 2 kaarten en 7 kaartlagen,
    # alle zeven met een gebiedsaanwijzing als anker.
    ("p2p.kaart", ["identificatie"], []),
    ("p2p.kaartlaag", ["id"],
     [("kaart_id", "p2p.kaart", "identificatie"),
      ("activiteit_id", "p2p.activiteit", "identificatie"),
      ("gebiedsaanwijzing_id", "p2p.gebiedsaanwijzing", "identificatie"),
      ("norm_id", "p2p.norm", "identificatie")]),

    # En de pons. Die staat hier omdat de eerste conclusie over de 47
    # ongerefereerde locaties te snel was: drie ervan dragen wél een pons, en
    # pons is precies wat ponsenkaart.nl toont. "Hangt nergens aan" gold voor
    # activiteit, gebiedsaanwijzing, tekstdeel en normwaarde -- niet voor pons.
    ("p2p.pons", ["identificatie"],
     [("locatie_id", "p2p.locatie", "identificatie")]),
]

# De locatie-scope is niet "alle locaties" maar alleen wat deze keten nodig heeft:
# de locaties onder de ontbrekende gebiedsaanwijzingen en tekstdelen. Anders zou
# dit script de complete locatietabel gaan vergelijken (honderdduizenden rijen
# met geometrie) voor een gat van enkele tientallen.
LOCATIE_SCOPE = """
    SELECT DISTINCT locatie_id FROM (
        SELECT locatie_id FROM p2p.gebiedsaanwijzing
        UNION ALL
        SELECT locatie_id FROM p2p.tekstdeel
        UNION ALL
        SELECT locatie_id FROM p2p.pons
    ) x WHERE locatie_id IS NOT NULL
"""


def lokale_dsn() -> str:
    return (f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")


def sleutels(cur, tabel: str, pk: list[str], beperking: str = "") -> set[tuple]:
    cur.execute(f"SELECT {', '.join(pk)} FROM {tabel} {beperking}")
    return {tuple(r) for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ja", action="store_true", help="echt kopiëren (default: droogloop)")
    a = ap.parse_args()

    prod_dsn = os.getenv("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2

    with psycopg.connect(lokale_dsn(), connect_timeout=20) as lconn, \
         psycopg.connect(prod_dsn, connect_timeout=30) as pconn:
        lc, pc = lconn.cursor(), pconn.cursor()
        # Railway-container heeft een kleine /dev/shm; parallellisme uit.
        pc.execute("SET max_parallel_workers_per_gather = 0")
        pc.execute("SET max_parallel_maintenance_workers = 0")

        totaal, totaal_wees = 0, 0
        for tabel, pk, fks in KETEN:
            t0 = time.time()
            beperking = (f"WHERE identificatie IN ({LOCATIE_SCOPE})"
                         if tabel == "p2p.locatie" else "")
            lok = sleutels(lc, tabel, pk, beperking)
            prod = sleutels(pc, tabel, pk, beperking)
            ontbreekt = lok - prod

            if not ontbreekt:
                print(f"  {tabel:34} gelijk ({len(lok):,})")
                continue

            # De ontbrekende sleutels als tijdelijke tabel aan de lokale kant.
            # `(a,b) IN %s` bestaat niet in psycopg3 en een VALUES-lijst uitvouwen
            # wordt bij duizenden sleutels onwerkbaar; een join is simpeler én
            # laat samengestelde sleutels vanzelf werken.
            lc.execute("DROP TABLE IF EXISTS bf_sleutels")
            lc.execute(f"CREATE TEMP TABLE bf_sleutels AS "
                       f"SELECT {', '.join(pk)} FROM {tabel} WITH NO DATA")
            with lconn.cursor().copy(
                    f"COPY bf_sleutels ({', '.join(pk)}) FROM STDIN") as cp:
                for sleutel in ontbreekt:
                    cp.write_row(sleutel)
            join = " AND ".join(f"t.{k} = s.{k}" for k in pk)
            waar = f"FROM {tabel} t JOIN bf_sleutels s ON {join}"

            # Welke ouders bestaan op prod? Ook die zetten we lokaal in een
            # tijdelijke tabel, zodat de COPY-query straks parameterloos is —
            # `COPY (…) TO STDOUT` accepteert geen placeholders en psycopg3's
            # gewone Cursor kent geen mogrify() om ze vooraf in te vullen.
            testen = []
            for fk_kol, ouder, ouder_pk in fks:
                lc.execute(f"SELECT DISTINCT t.{fk_kol} {waar} WHERE t.{fk_kol} IS NOT NULL")
                nodig = sorted({r[0] for r in lc.fetchall()})
                pc.execute(f"SELECT {ouder_pk} FROM {ouder} WHERE {ouder_pk} = ANY(%s)",
                           (nodig,))
                aanwezig = [r[0] for r in pc.fetchall()]
                hulp = f"bf_ouder_{fk_kol}"
                lc.execute(f"DROP TABLE IF EXISTS {hulp}")
                lc.execute(f"CREATE TEMP TABLE {hulp} (k text PRIMARY KEY)")
                with lconn.cursor().copy(f"COPY {hulp} (k) FROM STDIN") as cp:
                    for k in aanwezig:
                        cp.write_row((k,))
                testen.append(f"(t.{fk_kol} IS NULL OR EXISTS "
                              f"(SELECT 1 FROM {hulp} h WHERE h.k = t.{fk_kol}))")

            kols = kolommen(lc, tabel)
            fk_test = " AND ".join(testen) or "true"

            lc.execute(f"SELECT count(*) {waar} WHERE {fk_test}")
            bruikbaar = lc.fetchone()[0]
            wezen = len(ontbreekt) - bruikbaar
            totaal_wees += wezen

            melding = (f"  {tabel:34} mist {len(ontbreekt):>6,}  bruikbaar {bruikbaar:>6,}"
                       f"  ouder ontbreekt {wezen:>5,}")
            if not a.ja:
                print(melding + "   (droogloop)")
                totaal += bruikbaar
                continue

            kol_csv = ", ".join(f'"{k}"' for k in kols)
            stg = "stg_bf_" + tabel.split(".")[1]
            pc.execute(f"CREATE TEMP TABLE {stg} ON COMMIT DROP AS "
                       f"SELECT {kol_csv} FROM {tabel} WITH NO DATA")
            bron = (f"SELECT {', '.join(f't."{k}"' for k in kols)} {waar} "
                    f"WHERE {fk_test}")
            with lconn.cursor().copy(f"COPY ({bron}) TO STDOUT (FORMAT TEXT)") as uit:
                with pc.copy(f"COPY {stg} ({kol_csv}) FROM STDIN (FORMAT TEXT)") as inn:
                    for blok in uit:
                        inn.write(blok)
            # p2p.kaartlaag.id is GENERATED ALWAYS. De lokale id meenemen in
            # plaats van prod een nieuwe laten uitdelen: dezelfde reden als in
            # repliceer_p2p_naar_prod.py -- twee kanten met verschillende id's
            # voor dezelfde rij maken elke latere vergelijking onbruikbaar.
            idents = identity_kolommen(pc, tabel)
            overriding = " OVERRIDING SYSTEM VALUE" if any(a for _, a in idents) else ""
            pc.execute(f"INSERT INTO {tabel} ({kol_csv}){overriding} "
                       f"SELECT {kol_csv} FROM {stg} "
                       f"ON CONFLICT DO NOTHING RETURNING 1")
            n = len(pc.fetchall())
            for kol, _ in idents:
                # sequence meeschuiven, anders botst de eerstvolgende insert
                pc.execute("SELECT setval(pg_get_serial_sequence(%s, %s), "
                           f"  coalesce((SELECT max({kol}) FROM {tabel}), 1))",
                           (tabel, kol))
            pconn.commit()
            totaal += n
            print(melding + f"  → +{n:,} ({time.time() - t0:.1f}s)")

        print(f"\n{'Klaar' if a.ja else 'DROOGLOOP'} — {totaal:,} rijen "
              f"{'gekopieerd' if a.ja else 'aan te bieden'}; "
              f"{totaal_wees:,} overgeslagen wegens ontbrekende ouder.")
        if not a.ja:
            print("Draai opnieuw met --ja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
