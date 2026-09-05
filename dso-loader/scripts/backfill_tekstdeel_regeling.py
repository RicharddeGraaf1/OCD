#!/usr/bin/env python
"""Vul `p2p.tekstdeel.regeling_expression` uit de lokale OW-ZIP-cache.

Waarom dit kan zonder één API-call
----------------------------------
Een tekstdeel komt uit `OW-bestanden/tekstdelen.xml` binnen de OW-ZIP van één
regeling. Die ZIPs staan al op schijf (`data/downloads/ow`, ~1.900 stuks), dus de
herkomst is er nog — hij is bij het laden alleen nooit vastgelegd. Het parsen van
de hele cache kost **1,0 seconde**: de tekstdelen-XML is klein en de meeste ZIPs
hebben er geen (466 van 1.898).

Waarom de kolom er moest komen: zie vault-G-141 en
`scripts/2026-09-add-tekstdeel-regeling-expression.sql`.

Wat dit script NIET kan
-----------------------
De cache bewaart per *work* één bestand, dus een nieuwe versie overschrijft de
vorige. Tekstdelen van verdrongen expressies zijn daardoor niet meer te
herleiden. Gemeten op 2026-09-05: **23.021 van 27.817 (82,8%)** te mappen, 4.796
niet. Die laatste horen bij oudere expressies en blijven `NULL` — dat is een
eerlijke "herkomst onbekend", geen gok.

Een tekstdeel hoort bij precies één regeling: van de 24.198 unieke ids in de
cache kwamen er 23 in twee ZIPs voor, en dat bleken alle 23 dezelfde regeling
onder twee bestandsnamen (een AKN-naam en een handmatige kopie). Vandaar een
gewone kolom en geen junctie-tabel.

Droogloop is de default; `--ja` schrijft.
"""
from __future__ import annotations

import argparse
import collections
import os
import pathlib
import sys
import time
import zipfile

HIER = pathlib.Path(__file__).resolve().parent
ROOT = HIER.parent
# Zonder dit werkt het script alleen vanuit full_sync.py — dezelfde val als
# G-125/G-129 en als `vul_locatie_generalisatie.py` op 2026-09-05.
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402

from src.parsers.ow_xml import parse_tekstdelen  # noqa: E402

ZIPDIR = ROOT / "data" / "downloads" / "ow"


def lokale_dsn() -> str:
    return (f"host={os.getenv('DB_HOST')} port={os.getenv('DB_PORT')} "
            f"dbname={os.getenv('DB_NAME')} user={os.getenv('DB_USER')} "
            f"password={os.getenv('DB_PASSWORD')}")


def mapping_uit_cache() -> tuple[dict[str, str], dict[str, int]]:
    """tekstdeel-identificatie → frbr_work, plus wat statistiek.

    De ZIP-naam draagt het *work* (`akn_nl_act_gm0014_2020_omgevingsplan.zip`),
    niet de expressie. De expressie halen we er in de database bij via
    `p2p.regeling`, zodat we de vigerende versie pakken en niet gokken.
    """
    per_id: dict[str, str] = {}
    dubbel: collections.Counter[str] = collections.Counter()
    stat = {"zips": 0, "met_tekstdelen": 0, "tekstdelen": 0}

    for z in sorted(ZIPDIR.glob("*.zip")):
        stat["zips"] += 1
        try:
            with zipfile.ZipFile(z) as zf:
                if "OW-bestanden/tekstdelen.xml" not in zf.namelist():
                    continue
                tds = parse_tekstdelen(zf.read("OW-bestanden/tekstdelen.xml"))
        except Exception as e:  # kapotte ZIP: overslaan en tellen, niet stoppen
            print(f"  ZIP overgeslagen ({type(e).__name__}): {z.name}")
            continue
        if not tds:
            continue
        stat["met_tekstdelen"] += 1
        stat["tekstdelen"] += len(tds)
        # `akn_nl_act_gm0014_2020_omgevingsplan` → `/akn/nl/act/gm0014/2020/omgevingsplan`
        #
        # Alleen de eerste vijf underscores worden slashes. Een AKN-work heeft
        # precies zes segmenten en het laatste (de naam) mag zelf underscores
        # bevatten: `akn_nl_act_gm0034_2025_2_9` is `/akn/nl/act/gm0034/2025/2_9`
        # en niet `.../2025/2/9`. Een kale replace liet 310 van de 1.894 ZIPs
        # op een niet-bestaand work uitkomen — gemeten 2026-09-05.
        work = "/" + "/".join(z.stem.split("_", 5)) if z.stem.startswith("akn_") else None
        for td in tds:
            dubbel[td["identificatie"]] += 1
            if work:
                per_id[td["identificatie"]] = work
    return per_id, {**stat, "dubbel": sum(1 for v in dubbel.values() if v > 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ja", action="store_true", help="echt schrijven")
    args = ap.parse_args()

    if not ZIPDIR.is_dir():
        print(f"ZIP-cache niet gevonden: {ZIPDIR}")
        return 2

    t0 = time.time()
    per_id, stat = mapping_uit_cache()
    print(f"{stat['zips']} ZIPs geparsed in {time.time() - t0:.1f}s — "
          f"{stat['met_tekstdelen']} met tekstdelen, {stat['tekstdelen']:,} tekstdelen, "
          f"{len(per_id):,} unieke ids met een work ({stat['dubbel']} in meer dan één ZIP)")

    with psycopg.connect(lokale_dsn()) as conn, conn.cursor() as cur:
        # work → vigerende expressie. Inactieve expressies vallen af, zodat een
        # tekstdeel nooit naar een verdrongen versie gaat wijzen.
        cur.execute("""SELECT frbr_work, frbr_expression FROM p2p.regeling
                        WHERE inactief IS NOT TRUE""")
        work_expr = dict(cur.fetchall())

        koppel = [(per_id[i], i) for i in per_id if per_id[i] in work_expr]
        rijen = [(work_expr[w], i) for w, i in koppel]
        print(f"  waarvan een vigerende expressie bekend is: {len(rijen):,}")

        cur.execute("SELECT count(*) FROM p2p.tekstdeel")
        totaal = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM p2p.tekstdeel WHERE regeling_expression IS NOT NULL")
        al_gevuld = cur.fetchone()[0]
        print(f"  tekstdelen in de DB: {totaal:,} (nu {al_gevuld:,} gevuld)")

        if not args.ja:
            cur.execute("""SELECT count(*) FROM p2p.tekstdeel td
                            WHERE td.identificatie = ANY(%s)""",
                        ([i for _, i in rijen],))
            raakt = cur.fetchone()[0]
            print(f"\nDROOGLOOP — zou {raakt:,} van {totaal:,} tekstdelen vullen "
                  f"({100 * raakt / totaal:.1f}%). Draai opnieuw met --ja.")
            return 0

        t1 = time.time()
        cur.execute("CREATE TEMP TABLE stg_td (expr TEXT, ident TEXT) ON COMMIT DROP")
        with cur.copy("COPY stg_td (expr, ident) FROM STDIN") as cp:
            for expr, ident in rijen:
                cp.write_row((expr, ident))
        cur.execute("""UPDATE p2p.tekstdeel td SET regeling_expression = s.expr
                         FROM stg_td s WHERE td.identificatie = s.ident
                          AND td.regeling_expression IS DISTINCT FROM s.expr""")
        n = cur.rowcount
        conn.commit()

        cur.execute("SELECT count(*) FROM p2p.tekstdeel WHERE regeling_expression IS NOT NULL")
        na = cur.fetchone()[0]
        print(f"\nKlaar — {n:,} rijen bijgewerkt in {time.time() - t1:.1f}s. "
              f"Gevuld: {al_gevuld:,} → {na:,} van {totaal:,} ({100 * na / totaal:.1f}%). "
              f"De resterende {totaal - na:,} horen bij expressies waarvan de ZIP is "
              f"overschreven door een nieuwere versie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
