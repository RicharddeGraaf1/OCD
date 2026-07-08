"""Backfill: corrigeer verkeerd gekozen geometrie bij records met meerdere
gebiedsmarkeringen (zie vault gaps.md G-87).

KOOP levert soms meerdere <ow:gebiedsmarkering>-blokken per record; de oude
loader nam blind het eerste (`.find()`), wat een fout punt kon zijn (bv. de
Woerden-pin op een Amsterdams adres). De nieuwe selectie in
src/loaders/koop_vergunning.py kiest de betrouwbaarste kandidaat. Dit script
her-selecteert uit de reeds opgeslagen `raw_xml` — GEEN her-fetch bij KOOP —
en werkt de geometrie-/adreskolommen bij waar de keuze wijzigt.

Twee-fasen (aparte connecties): eerst read-only streamen + wijzigingen
verzamelen, daarna in een tweede connectie wegschrijven. Zo kan een commit
nooit de server-side leescursor sluiten (de bug in de vorige versie).

Gebruik:
    python scripts/koop-poc/backfill_geometrie.py            # dry-run (leest, schrijft niet)
    python scripts/koop-poc/backfill_geometrie.py --apply    # voert UPDATEs uit
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db import get_conn  # noqa: E402
from src.loaders.koop_vergunning import (  # noqa: E402
    NS, build_gemeente_centroids, parse_gebiedsmarkering, set_gemeente_centroids,
)

APPLY = "--apply" in sys.argv
CHANGE_THRESHOLD_M = 1.0  # alleen bijwerken als het gekozen punt >1 m verschuift


def build_centroids() -> dict[str, tuple[float, float]]:
    """Gemeente -> mediane RD-centroïde uit de betrouwbare single-geometrie-corpus.

    Deelt de bron met de live ingest (build_gemeente_centroids in de loader),
    zodat backfill en ingest exact dezelfde selectie voeden.
    """
    conn = get_conn()
    try:
        return build_gemeente_centroids(conn)
    finally:
        conn.close()


def collect_changes(centroids: dict) -> tuple[list[dict], dict]:
    """Fase 1 — read-only: stream alle >=2-geom records en verzamel wijzigingen."""
    set_gemeente_centroids(centroids)
    conn = get_conn()  # eigen leesconnectie; nooit committen tijdens de stream
    read = conn.cursor()

    changes: list[dict] = []
    stats = {"seen": 0, "no_pick": 0,
             "buckets": {"1m-1km": 0, "1-5km": 0, "5-20km": 0, ">20km": 0, "was-null": 0},
             "samples": []}

    sql = r"""
        SELECT koop_id, raw_xml,
               ST_X(geometrie_rd_pt) AS cur_x, ST_Y(geometrie_rd_pt) AS cur_y
        FROM vth.vergunningkennisgeving
        WHERE regexp_count(raw_xml, '<([A-Za-z0-9]+:)?geometrie>') >= 2
    """
    for row in read.stream(sql):
        stats["seen"] += 1
        try:
            root = ET.fromstring(row["raw_xml"])
        except ET.ParseError:
            continue
        g = parse_gebiedsmarkering(root.findall(".//ow:gebiedsmarkering", NS))
        nx, ny = g.get("rd_x"), g.get("rd_y")
        if nx is None or ny is None:
            stats["no_pick"] += 1
            continue
        cx, cy = row["cur_x"], row["cur_y"]
        moved = None if (cx is None or cy is None) else \
            ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5
        if moved is not None and moved <= CHANGE_THRESHOLD_M:
            continue  # keuze onveranderd

        if moved is None:
            stats["buckets"]["was-null"] += 1
        else:
            km = moved / 1000
            b = ("1m-1km" if km < 1 else "1-5km" if km < 5
                 else "5-20km" if km < 20 else ">20km")
            stats["buckets"][b] += 1
        if len(stats["samples"]) < 20:
            stats["samples"].append(
                (row["koop_id"], g["ligt_in_gemeente"],
                 None if moved is None else round(moved / 1000, 1), g["geometrie_type"]))

        changes.append({
            "id": row["koop_id"],
            "gt": g["geometrie_type"],
            "rd": g.get("rd_wkt"),
            "rdpt": (f"POINT({nx} {ny})") if nx is not None and ny is not None else None,
            "wgspt": (f"POINT({g['lon']} {g['lat']})")
                     if g.get("lat") is not None and g.get("lon") is not None else None,
            "label": g["geometrielabel"], "postcode": g["postcode"],
            "huisnummer": g["huisnummer"], "huisletter": g["huisletter"],
            "toev": g["huisnummertoevoeging"], "straatnaam": g["straatnaam"],
            "woonplaats": g["woonplaats"], "gem": g["ligt_in_gemeente"],
        })
        if stats["seen"] % 20000 == 0:
            print(f"    …{stats['seen']:,} bekeken, {len(changes):,} wijzigingen", flush=True)

    conn.close()
    return changes, stats


UPDATE_SQL = """
    UPDATE vth.vergunningkennisgeving SET
        geometrie_type = %(gt)s,
        geometrie_rd = ST_GeomFromText(%(rd)s::text, 28992),
        geometrie_rd_pt = ST_GeomFromText(%(rdpt)s::text, 28992),
        geometrie_wgs_pt = ST_GeomFromText(%(wgspt)s::text, 4326),
        geometrielabel = %(label)s,
        postcode = %(postcode)s,
        huisnummer = %(huisnummer)s,
        huisletter = %(huisletter)s,
        huisnummertoevoeging = %(toev)s,
        straatnaam = %(straatnaam)s,
        woonplaats = %(woonplaats)s,
        ligt_in_gemeente = %(gem)s
    WHERE koop_id = %(id)s
"""


def apply_changes(changes: list[dict]) -> int:
    """Fase 2 — schrijf de wijzigingen weg op een aparte connectie."""
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    for ch in changes:
        cur.execute(UPDATE_SQL, ch)
        n += 1
        if n % 1000 == 0:
            conn.commit()
            print(f"    …{n:,}/{len(changes):,} weggeschreven", flush=True)
    conn.commit()
    conn.close()
    return n


def main() -> None:
    print("Fase 0 — bouw gemeente-centroïde-map uit single-geometrie-records…", flush=True)
    centroids = build_centroids()
    print(f"  {len(centroids):,} gemeenten met centroïde.", flush=True)

    print("Fase 1 — her-selecteer geometrie voor records met >=2 geometrieën…", flush=True)
    changes, stats = collect_changes(centroids)

    print("\n=== Analyse ===")
    print(f"  records met >=2 geometrieën bekeken: {stats['seen']:,}")
    print(f"  geen geometrie te kiezen:            {stats['no_pick']:,}")
    print(f"  keuze WIJZIGT (>{CHANGE_THRESHOLD_M:g} m):          {len(changes):,}")
    print("  verschuiving t.o.v. oude opgeslagen punt:")
    for k, v in stats["buckets"].items():
        print(f"    {k:>8}: {v:>7,}")
    print("\n  voorbeelden (koop_id, gemeente, verschuiving_km, nieuw_type):")
    for s in stats["samples"]:
        print(f"    {s}")

    if APPLY:
        print(f"\nFase 2 — {len(changes):,} UPDATEs uitvoeren…", flush=True)
        n = apply_changes(changes)
        print(f"  klaar: {n:,} records bijgewerkt.", flush=True)
    else:
        print("\n  (dry-run — niets geschreven; draai met --apply)")

    # controle
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT ST_AsText(geometrie_rd_pt) p, geometrie_type t, ligt_in_gemeente g "
                "FROM vth.vergunningkennisgeving WHERE koop_id=%s", ("gmb-2026-173404",))
    r = cur.fetchone()
    print(f"\n  gmb-2026-173404 nu in DB: {r['p']} ({r['t']}, {r['g']})"
          f"{'' if APPLY else '  ← ongewijzigd in dry-run'}")
    conn.close()


if __name__ == "__main__":
    main()
