"""Bouw p2p.locatie_generalisatie op uit p2p.locatie_subdiv.

Voorberekende, vereenvoudigde geometrie voor de vector-tile-laag. Zie
OCDviewer docs/plans/vector-tiles.md en het DDL-script
scripts/2026-08-08-add-locatie-generalisatie.sql.

Per niveau gebeuren er twee dingen:

  1. vereenvoudigen op de resolutie van dat zoomniveau
     (ST_SimplifyPreserveTopology), en
  2. weglaten wat volledig binnen een pixel valt — een vlak waarvan zowel de
     breedte als de hoogte kleiner is dan de resolutie is op dat niveau niet
     te zien, en kost wel bytes in elke tegel.

Vollédige herbouw, geen incrementele detectie. Dat kan omdat het goedkoop
genoeg is, en het is aanzienlijk eenvoudiger dan wijzigingen opsporen:
p2p.locatie heeft geen tijdstempel en geen directe verwijzing naar een
regeling.

Chunking gaat op ctid-paginabereik. De brontabel heeft geen primaire sleutel,
en een ctid-bereik geeft een aaneengesloten scan (goedkoop) plus voortgang,
hervatbaarheid en — de reden dat het hier uitmaakt — parallelisme.

**Waarom parallel.** Het werk is CPU-gebonden (simplify + hash), niet
IO-gebonden: gemeten 10.000 bronpagina's in 12 s, met of zonder indexen op de
doeltabel. PostgreSQL kan er zelf niets aan doen — INSERT ... SELECT is
parallel-onveilig, dus dat plan draait altijd op één kern. Meerdere
verbindingen die elk hun eigen ctid-bereik doen, lost dat wel op.

Gebruik:
    cd dso-loader && PYTHONPATH=. .venv/Scripts/python scripts/vul_locatie_generalisatie.py
    ... --niveau 8              # alleen het grove niveau
    ... --steekproef 20000      # stop na N bronpagina's, om te meten
    ... --workers 8             # meer parallelle verbindingen
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import click
from rich.console import Console

from src.db import get_conn

console = Console()

# PDOK-RD-piramide: 3440.640 m/px op z0, elke stap gehalveerd.
# Niveau = het hoogste zoomniveau dat het bedient.
NIVEAUS: dict[int, float] = {
    6: 3440.640 / 2**6,   # 53,76 m — bedient z0 t/m z6
    8: 3440.640 / 2**8,   # 13,44 m — bedient z7 en z8
    10: 3440.640 / 2**10,  # 3,36 m — bedient z9 en z10
}

BLOKKEN_PER_CHUNK = 20_000  # ~6,5 rijen per pagina → ~130.000 rijen per chunk

# De indexen staan in het DDL-script, maar worden voor een herbouw weggegooid
# en achteraf opnieuw gelegd: een GIST-index bijhouden tijdens miljoenen
# inserts is duurder dan hem in één keer bouwen.
# Per niveau alleen zijn eigen partiele GIST-index, zodat het herbouwen van
# een enkel niveau de indexen van de andere niveaus niet aanraakt.
def _index_ddl(niveau: int) -> tuple[str, str]:
    naam = f"idx_locatie_gen_geom_n{niveau}"
    return naam, (
        f"CREATE INDEX {naam} ON p2p.locatie_generalisatie "
        f"USING gist (geometrie) WHERE niveau = {niveau}"
    )


# De koppeling tegel-feature -> object staat los van het niveau en wordt alleen
# herbouwd als alle niveaus opnieuw gevuld worden.
ID_INDEX = (
    "idx_locatie_gen_id",
    "CREATE INDEX idx_locatie_gen_id ON p2p.locatie_generalisatie "
    "(identificatie, niveau)",
)

# Vereenvoudig, gooi weg wat binnen een pixel past, en leg de vingerafdruk van
# de bron vast. De sub-pixeltoets gebruikt de bounding box (niet de
# oppervlakte): een lange dunne strook is wel degelijk zichtbaar, ook al is
# haar oppervlakte klein.
#
# Bewust GEEN validatie-reparatie hier. ST_SimplifyPreserveTopology belooft
# meer dan hij levert — hij bewaakt elke ring afzonderlijk, maar een gat kan
# daarbij buiten zijn schil belanden — en 0,4% van de vlakken komt er ongeldig
# uit. Getest of dat erg is, met een envelope die de vorm zeker omsluit: van
# 887 ongeldige vlakken verdwijnt er geen enkele uit de tegel, en de MVT-uitvoer
# is in alle gevallen geldig. ST_AsMVTGeom repareert dus zelf. Het wel doen
# (ST_IsValid + ST_MakeValid per rij) verdubbelde de bouwtijd — 0,8 naar 1,7 min
# op 120.000 bronpagina's — voor nul verschil in het resultaat.
INSERT_SQL = """
INSERT INTO p2p.locatie_generalisatie (identificatie, niveau, geometrie, bron_hash)
SELECT identificatie, %(niveau)s, g, bron_hash
  FROM (
    SELECT identificatie,
           ST_SimplifyPreserveTopology(geometrie, %(tol)s) AS g,
           md5(ST_AsBinary(geometrie))::uuid               AS bron_hash
      FROM p2p.locatie_subdiv
     WHERE ctid >= %(van)s::tid AND ctid < %(tot)s::tid
       AND NOT (ST_XMax(geometrie) - ST_XMin(geometrie) < %(tol)s
            AND ST_YMax(geometrie) - ST_YMin(geometrie) < %(tol)s)
  ) s
 WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
"""


def _paginas(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT relpages FROM pg_class WHERE oid = 'p2p.locatie_subdiv'::regclass")
        return cur.fetchone()["relpages"]


def _bouw_niveau(niveau: int, tol: float, laatste_blok: int, vanaf: int, workers: int) -> None:
    chunks = [
        (b, min(b + BLOKKEN_PER_CHUNK, laatste_blok + 1))
        for b in range(vanaf, laatste_blok + 1, BLOKKEN_PER_CHUNK)
    ]
    console.print(
        f"\n[bold cyan]Niveau {niveau}[/bold cyan] — tolerantie {tol:.2f} m, "
        f"{len(chunks)} chunks over {workers} verbindingen"
    )

    start = time.monotonic()
    stand = {"rijen": 0, "klaar": 0}
    slot = threading.Lock()
    lokaal = threading.local()

    def verbinding():
        if not hasattr(lokaal, "conn"):
            lokaal.conn = get_conn()
        return lokaal.conn

    def doe_chunk(chunk: tuple[int, int]) -> None:
        van, tot = chunk
        conn = verbinding()
        with conn.cursor() as cur:
            cur.execute(
                INSERT_SQL,
                {"niveau": niveau, "tol": tol, "van": f"({van},0)", "tot": f"({tot},0)"},
            )
            n = cur.rowcount
        conn.commit()

        with slot:
            stand["rijen"] += n
            stand["klaar"] += 1
            verstreken = time.monotonic() - start
            resterend = verstreken / stand["klaar"] * (len(chunks) - stand["klaar"])
            console.print(
                f"  {stand['klaar']:>3}/{len(chunks)} chunks  "
                f"{stand['rijen']:>10,} rijen  "
                f"{verstreken / 60:5.1f} min, nog ~{resterend / 60:.1f} min"
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(doe_chunk, chunks))

    console.print(
        f"[bold green]Niveau {niveau} klaar[/bold green]: {stand['rijen']:,} rijen in "
        f"{(time.monotonic() - start) / 60:.1f} min"
    )


def _te_bouwen_indexen(niveaus: dict[int, float], alles: bool) -> list[tuple[str, str]]:
    lijst = [_index_ddl(n) for n in niveaus]
    if alles:
        lijst.append(ID_INDEX)
    return lijst


def _zonder_indexen(conn, indexen: list[tuple[str, str]]) -> None:
    with conn.cursor() as cur:
        for naam, _ in indexen:
            cur.execute(f"DROP INDEX IF EXISTS p2p.{naam}")
    conn.commit()


def _met_indexen(conn, indexen: list[tuple[str, str]]) -> None:
    for naam, ddl in indexen:
        start = time.monotonic()
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS p2p.{naam}")
            cur.execute(ddl)
        conn.commit()
        console.print(f"  index {naam} in {time.monotonic() - start:.0f} s")


@click.command()
@click.option("--niveau", type=int, default=None, help="Alleen dit niveau (6, 8 of 10).")
@click.option(
    "--steekproef",
    type=int,
    default=None,
    help="Stop na dit aantal bronpagina's — om te meten zonder de hele tabel te doen.",
)
@click.option("--vanaf-blok", type=int, default=0, help="Hervat vanaf dit ctid-blok.")
@click.option(
    "--behoud",
    is_flag=True,
    help="Niet eerst leegmaken (alleen zinvol samen met --vanaf-blok).",
)
@click.option("--workers", type=int, default=6, help="Parallelle verbindingen.")
def main(
    niveau: int | None,
    steekproef: int | None,
    vanaf_blok: int,
    behoud: bool,
    workers: int,
) -> None:
    conn = get_conn()
    try:
        paginas = _paginas(conn)
        laatste_blok = (steekproef or paginas) - 1
        niveaus = {niveau: NIVEAUS[niveau]} if niveau else NIVEAUS

        console.print(
            f"[bold]p2p.locatie_subdiv[/bold]: {paginas:,} pagina's"
            + (f" — steekproef tot blok {laatste_blok:,}" if steekproef else "")
        )

        alles = niveau is None
        indexen = _te_bouwen_indexen(niveaus, alles=alles)
        _zonder_indexen(conn, indexen)

        # Bij een volledige herbouw TRUNCATE in plaats van DELETE: 7 miljoen
        # dode tuples laten de tabel met 2 GB opzwellen tot de autovacuum
        # bijtrekt. Bij een enkel niveau kan dat niet en is DELETE de prijs.
        if alles and not behoud:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE p2p.locatie_generalisatie")
            conn.commit()
            console.print("  tabel geleegd (TRUNCATE)")

        for n, tol in niveaus.items():
            if not behoud and not alles:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM p2p.locatie_generalisatie WHERE niveau = %s", (n,))
                    console.print(f"  {cur.rowcount:,} bestaande rijen op niveau {n} verwijderd")
                conn.commit()
            _bouw_niveau(n, tol, laatste_blok, vanaf_blok, workers)

        console.print("\n[bold]Indexen opnieuw bouwen[/bold]")
        _met_indexen(conn, indexen)

        with conn.cursor() as cur:
            cur.execute("ANALYZE p2p.locatie_generalisatie")
            cur.execute(
                """SELECT niveau, count(*) AS n,
                          sum(ST_NPoints(geometrie)) AS punten
                     FROM p2p.locatie_generalisatie GROUP BY niveau ORDER BY niveau"""
            )
            for r in cur.fetchall():
                console.print(
                    f"[bold]niveau {r['niveau']}[/bold]: {r['n']:,} rijen, "
                    f"{r['punten']:,} punten"
                )
            cur.execute(
                "SELECT pg_size_pretty(pg_total_relation_size('p2p.locatie_generalisatie')) AS s"
            )
            console.print(f"[bold]omvang[/bold]: {cur.fetchone()['s']}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
