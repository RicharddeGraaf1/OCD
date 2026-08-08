"""Meet wat één vector tile kost — uit de brontabel versus uit de
generalisatietabel.

Bouwt de tegel-envelope exact volgens de PDOK-RD-piramide die de viewer
gebruikt (EPSG:28992, origin linksboven, 3440.640 m/px op z0, 256 px). Dat is
de valkuil van dit onderwerp: ST_TileEnvelope gaat uit van Web Mercator en
levert hier tegels die niet op de BRT-achtergrond aansluiten.

Gebruik:
    cd dso-loader && PYTHONPATH=. .venv/Scripts/python scripts/meet_tegelkosten.py
    ... --x 136827 --y 455914 --zooms 6,8,10,12
"""

import os
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import click
from rich.console import Console
from rich.table import Table

from src.db import get_conn

console = Console()

RD_MINX = -285401.92
RD_MAXY = 903401.92
RES_Z0 = 3440.640
TEGEL_PX = 256

# Welk generalisatieniveau bedient welke zoom. None = rechtstreeks uit de bron.
NIVEAU_VOOR_ZOOM = {
    z: (6 if z <= 6 else 8 if z <= 8 else 10 if z <= 10 else None) for z in range(15)
}

MVT_SQL = """
WITH env AS (
  SELECT ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 28992) AS b
),
mvt AS (
  SELECT b.identificatie AS id,
         ST_AsMVTGeom(b.geometrie, env.b, 4096, 64, true) AS geom
    FROM {bron} b, env
   WHERE b.geometrie && env.b {filter}
)
SELECT count(*) AS n, length(ST_AsMVT(mvt, 'locaties', 4096, 'geom')) AS bytes
  FROM mvt WHERE geom IS NOT NULL
"""


def envelope(x: float, y: float, z: int) -> tuple[float, float, float, float]:
    """Envelope van de tegel waarin (x, y) valt, op zoomniveau z."""
    breedte = RES_Z0 / 2**z * TEGEL_PX
    kol = int((x - RD_MINX) // breedte)
    rij = int((RD_MAXY - y) // breedte)
    minx = RD_MINX + kol * breedte
    maxy = RD_MAXY - rij * breedte
    return minx, maxy - breedte, minx + breedte, maxy


def meet(conn, bron: str, filter_: str, env: tuple[float, float, float, float]) -> tuple:
    sql = MVT_SQL.format(bron=bron, filter=filter_)
    params = dict(zip(("minx", "miny", "maxx", "maxy"), env))
    with conn.cursor() as cur:
        cur.execute(sql, params)  # warm-up: eerste keer is schijftijd, niet rekentijd
        conn.commit()
        start = time.monotonic()
        cur.execute(sql, params)
        r = cur.fetchone()
    conn.commit()
    return r["n"], r["bytes"] or 0, (time.monotonic() - start) * 1000


@click.command()
@click.option("--x", type=float, default=136827, help="RD X van het meetpunt.")
@click.option("--y", type=float, default=455914, help="RD Y van het meetpunt.")
@click.option("--zooms", default="6,8,10,12", help="Zoomniveaus, komma-gescheiden.")
def main(x: float, y: float, zooms: str) -> None:
    conn = get_conn()
    try:
        tabel = Table(title=f"Tegel rond RD {x:.0f}, {y:.0f} — warme cache")
        for kop in ("zoom", "breedte", "bron", "vlakken", "tegel", "tijd"):
            tabel.add_column(kop, justify="right" if kop != "bron" else "left")

        for z in [int(s) for s in zooms.split(",")]:
            env = envelope(x, y, z)
            breedte = f"{(env[2] - env[0]) / 1000:.1f} km"

            n, b, ms = meet(conn, "p2p.locatie_subdiv", "", env)
            tabel.add_row(f"z{z}", breedte, "subdiv", f"{n:,}", f"{b / 1024:,.0f} kB", f"{ms:,.0f} ms")

            niveau = NIVEAU_VOOR_ZOOM[z]
            if niveau is not None:
                n, b, ms = meet(
                    conn, "p2p.locatie_generalisatie", f"AND b.niveau = {niveau}", env
                )
                tabel.add_row(
                    "", "", f"generalisatie n{niveau}", f"{n:,}",
                    f"{b / 1024:,.0f} kB", f"{ms:,.0f} ms",
                )

        console.print(tabel)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
