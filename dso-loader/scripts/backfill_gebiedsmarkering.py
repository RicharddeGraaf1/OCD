"""Backfill van gemiste gebiedsmarkering-geometrie in vth.vergunningkennisgeving.

Aanleiding (2026-09-20). De parser accepteerde alleen de locatietypen
Adres/Punt/Vlak/Postcodegebied onder <ow:gebiedsmarkering>. KOOP levert er
meer — GeometrieRef, Weg, Perceel, Lijn, Woonplaats, Buurt, Wijk,
Hectometerpaal — allemaal met dezelfde <ow:geometrie> (WKT, RD) en
<ow:locatiegebied> (WKT, WGS84) eronder. Die vielen stil op de grond:
41.049 records stonden als "geen geometrie" in de DB terwijl de WKT gewoon
in de opgeslagen raw_xml zat. Daaronder alle 16.512 vergunningen die óók een
STOP-omgevingsdocument zijn (die leveren hun vergunningsgebied als
<ow:GeometrieRef>).

Geen her-crawl nodig: dit script leest uitsluitend de eigen raw_xml en
hergebruikt parse_gebiedsmarkering uit de loader, zodat backfill en nieuwe
runs gegarandeerd dezelfde uitkomst geven.

Gebruik:
    python scripts/backfill_gebiedsmarkering.py --dry-run [--limit N]
    python scripts/backfill_gebiedsmarkering.py --churn  [--limit N]
    python scripts/backfill_gebiedsmarkering.py --apply

--dry-run  telt wat de fix oplevert op de records zonder geometrie.
--churn    toetst de records die al een geometrie hebben: zou herparsen hun
           pin verplaatsen? Dat hoort ~0 te zijn; is het dat niet, dan raakt
           de fix bestaande data en moet je eerst kijken waarom.
--apply    zet de CHECK-constraint ruimer en vult alleen de NULL-records.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg.rows import tuple_row  # noqa: E402

from src.db import get_conn  # noqa: E402
from src.loaders.koop_vergunning import NS, parse_gebiedsmarkering  # noqa: E402

BATCH = 2000

GEOM_TYPES = (
    "'Adres', 'Punt', 'Vlak', 'Postcodegebied', 'PostcodeHuisnummer', "
    "'GeometrieRef', 'Weg', 'Perceel', 'Lijn', 'Woonplaats', "
    "'Buurt', 'Wijk', 'Hectometerpaal'"
)

ALTER_CHECK = f"""
ALTER TABLE vth.vergunningkennisgeving
  DROP CONSTRAINT IF EXISTS vergunningkennisgeving_geometrie_type_check;
ALTER TABLE vth.vergunningkennisgeving
  ADD CONSTRAINT vergunningkennisgeving_geometrie_type_check
  CHECK (geometrie_type IS NULL OR geometrie_type IN ({GEOM_TYPES}));
"""

UPDATE_SQL = """
UPDATE vth.vergunningkennisgeving SET
  geometrie_type  = %(geometrie_type)s,
  geometrie_rd    = ST_GeomFromText(%(rd_wkt)s, 28992),
  geometrie_rd_pt = ST_GeomFromText(%(rd_pt)s, 28992),
  geometrie_wgs_pt= ST_GeomFromText(%(wgs_pt)s, 4326),
  geometrielabel  = COALESCE(geometrielabel, %(geometrielabel)s),
  postcode        = COALESCE(postcode, %(postcode)s),
  huisnummer      = COALESCE(huisnummer, %(huisnummer)s),
  straatnaam      = COALESCE(straatnaam, %(straatnaam)s),
  woonplaats      = COALESCE(woonplaats, %(woonplaats)s),
  ligt_in_gemeente= COALESCE(ligt_in_gemeente, %(ligt_in_gemeente)s)
WHERE koop_id = %(koop_id)s
"""


def geom_uit_xml(raw_xml: str, titel: str | None = None) -> dict | None:
    """Herparse de gebiedsmarkeringen van één record; None als er niets in zit."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None
    gebieden = root.findall(".//ow:gebiedsmarkering", NS)
    if not gebieden:
        return None
    geom = parse_gebiedsmarkering(gebieden, titel=titel)
    if not geom.get("rd_wkt"):
        return None
    return geom


def _pin(geom: dict) -> tuple[str | None, str | None]:
    rd_pt = (
        f"POINT({geom['rd_x']} {geom['rd_y']})"
        if geom.get("rd_x") is not None and geom.get("rd_y") is not None
        else None
    )
    wgs_pt = (
        f"POINT({geom['lon']} {geom['lat']})"
        if geom.get("lon") is not None and geom.get("lat") is not None
        else None
    )
    return rd_pt, wgs_pt


def _iter_rows(conn, sql: str, limit: int | None):
    """Server-side cursor. get_conn() staat op dict_row, dus dat forceren we
    hier terug naar tuples — anders levert het uitpakken de kolomnamen op."""
    with conn.cursor(name="bf", row_factory=tuple_row) as cur:
        cur.itersize = BATCH
        cur.execute(sql + (f" LIMIT {int(limit)}" if limit else ""))
        for row in cur:
            yield row


def dry_run(conn, limit: int | None) -> None:
    sql = """SELECT koop_id, raw_xml FROM vth.vergunningkennisgeving
             WHERE geometrie_rd IS NULL"""
    n = gevonden = zonder = 0
    types: Counter = Counter()
    for koop_id, raw_xml in _iter_rows(conn, sql, limit):
        n += 1
        geom = geom_uit_xml(raw_xml or "")
        if geom is None:
            zonder += 1
            continue
        gevonden += 1
        types[str(geom["geometrie_type"])] += 1
    print(f"records zonder geometrie bekeken : {n:>8,}".replace(",", "."))
    print(f"waarvan alsnog een WKT gevonden  : {gevonden:>8,}".replace(",", "."))
    print(f"echt zonder geometrie            : {zonder:>8,}".replace(",", "."))
    print("\nlocatietype van de vangst:")
    for tag, cnt in types.most_common():
        print(f"  {tag:<16} {cnt:>8,}".replace(",", "."))


def churn(conn, limit: int | None) -> None:
    """Zou herparsen de pin van bestaande records verplaatsen?"""
    sql = """SELECT koop_id, raw_xml, ST_X(geometrie_rd_pt), ST_Y(geometrie_rd_pt),
                    geometrie_type, titel
             FROM vth.vergunningkennisgeving
             WHERE geometrie_rd_pt IS NOT NULL ORDER BY koop_id"""
    n = gelijk = verplaatst = type_flip = weg = 0
    grootste = 0.0
    for koop_id, raw_xml, x, y, ou_type, titel in _iter_rows(conn, sql, limit):
        n += 1
        geom = geom_uit_xml(raw_xml or "", titel)
        if geom is None:
            weg += 1
            continue
        if str(geom["geometrie_type"]) != (ou_type or ""):
            type_flip += 1
        nx, ny = geom.get("rd_x"), geom.get("rd_y")
        if nx is None or ny is None:
            weg += 1
            continue
        d = math.hypot(nx - x, ny - y)
        grootste = max(grootste, d)
        if d < 0.5:
            gelijk += 1
        else:
            verplaatst += 1
    print(f"records met geometrie bekeken : {n:>8,}".replace(",", "."))
    print(f"pin blijft gelijk (<0,5 m)    : {gelijk:>8,}".replace(",", "."))
    print(f"pin verschuift                : {verplaatst:>8,}".replace(",", "."))
    print(f"locatietype verandert         : {type_flip:>8,}".replace(",", "."))
    print(f"zou geometrie verliezen       : {weg:>8,}".replace(",", "."))
    print(f"grootste verschuiving         : {grootste:.2f} m")


def apply(conn) -> None:
    conn.execute(ALTER_CHECK)
    conn.commit()
    print("CHECK-constraint verruimd")

    # Keyset-paginatie in plaats van één server-side cursor: we committen per
    # batch, en een commit sluit een named cursor (die bestaat niet WITH HOLD).
    sql = """SELECT koop_id, raw_xml, titel FROM vth.vergunningkennisgeving
             WHERE geometrie_rd IS NULL AND koop_id > %s
             ORDER BY koop_id LIMIT %s"""
    laatste = ""
    n = bij = 0
    types: Counter = Counter()
    while True:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(sql, (laatste, BATCH))
            rows = cur.fetchall()
        if not rows:
            break
        with conn.cursor(row_factory=tuple_row) as wcur:
            for koop_id, raw_xml, titel in rows:
                n += 1
                laatste = koop_id
                geom = geom_uit_xml(raw_xml or "", titel)
                if geom is None:
                    continue
                rd_pt, wgs_pt = _pin(geom)
                wcur.execute(UPDATE_SQL, {
                    "koop_id": koop_id,
                    "geometrie_type": geom["geometrie_type"],
                    "rd_wkt": geom["rd_wkt"],
                    "rd_pt": rd_pt,
                    "wgs_pt": wgs_pt,
                    "geometrielabel": geom["geometrielabel"],
                    "postcode": geom["postcode"],
                    "huisnummer": geom["huisnummer"],
                    "straatnaam": geom["straatnaam"],
                    "woonplaats": geom["woonplaats"],
                    "ligt_in_gemeente": geom["ligt_in_gemeente"],
                })
                bij += 1
                types[str(geom["geometrie_type"])] += 1
        conn.commit()
        print(f"  {n:>7,} bekeken, {bij:>7,} bijgewerkt".replace(",", "."))
    print()
    print(f"bekeken   : {n:>8,}".replace(",", "."))
    print(f"bijgewerkt: {bij:>8,}".replace(",", "."))
    for tag, cnt in types.most_common():
        print(f"  {tag:<16} {cnt:>8,}".replace(",", "."))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--churn", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if not (args.dry_run or args.churn or args.apply):
        ap.error("kies --dry-run, --churn of --apply")

    with get_conn() as conn:
        conn.execute("SET statement_timeout = '600s'")
        if args.dry_run:
            dry_run(conn, args.limit)
        if args.churn:
            churn(conn, args.limit)
        if args.apply:
            apply(conn)


if __name__ == "__main__":
    main()
