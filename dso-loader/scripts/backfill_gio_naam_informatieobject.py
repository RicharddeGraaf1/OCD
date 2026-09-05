"""Backfill p2p.geo_informatieobject.naam_informatieobject uit lokale Download-ZIP's.

Leest per ZIP in data/downloads/ow/ de officiële `naamInformatieObject` (uit
IO-<uuid>/Metadata.xml) en zet die op de bijbehorende GIO-rij. Lichtgewicht:
alleen de naam-kolom, geen basisgeo-herinsert.

Run:  python scripts/backfill_gio_naam_informatieobject.py
      python scripts/backfill_gio_naam_informatieobject.py --dir data/downloads/ow
"""

import pathlib
import sys
from __future__ import annotations

import argparse
from pathlib import Path

# Zonder deze twee regels werkt dit script alleen vanuit een aanroeper die
# de repo-root al op sys.path heeft gezet (in de praktijk: full_sync.py).
# Een directe aanroep viel om op `ModuleNotFoundError: No module named 'src'`
# — zie vault G-125/G-129. Een `sys.path.insert(0, ".")` is geen alternatief:
# dat hangt af van de map waar je toevallig staat.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.db import get_conn
from src.loaders.gio_zip import extract_gio_naam_informatieobject


def backfill(downloads_dir: Path) -> None:
    zips = sorted(downloads_dir.glob("*.zip"))
    print(f"{len(zips)} ZIP's in {downloads_dir}")

    conn = get_conn()
    total_zip = 0
    total_set = 0
    total_names = 0
    try:
        with conn.cursor() as cur:
            for zp in zips:
                try:
                    naam_io = extract_gio_naam_informatieobject(zp)
                except Exception as e:  # corrupte ZIP overslaan, niet de hele run stoppen
                    print(f"  ! {zp.name}: {type(e).__name__}: {e}")
                    continue
                if not naam_io:
                    continue
                total_zip += 1
                total_names += len(naam_io)
                for frbr, naam in naam_io.items():
                    cur.execute(
                        """UPDATE p2p.geo_informatieobject
                           SET naam_informatieobject = %s
                           WHERE frbr_expression = %s""",
                        (naam, frbr),
                    )
                    total_set += cur.rowcount
                conn.commit()
        print(f"Klaar: {total_zip} ZIP's met namen, {total_names} namen geëxtraheerd, "
              f"{total_set} GIO-rijen bijgewerkt.")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/downloads/ow", help="map met Download-ZIP's")
    args = ap.parse_args()
    backfill(Path(args.dir))
