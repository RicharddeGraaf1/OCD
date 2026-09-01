"""Bouw de generalisatietabellen voor de vector-tile-lagen.

Twee bronnen, identieke bewerking (`--bron`):

    ow   p2p.locatie_subdiv  ->  p2p.locatie_generalisatie
    wro  wro.planobject      ->  wro.planobject_generalisatie

Zie OCDviewer docs/plans/vector-tiles.md en de DDL-scripts
2026-08-08-add-locatie-generalisatie.sql / 2026-08-09-add-planobject-generalisatie.sql.

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

# Twee bronnen, identieke bewerking. Wro is niet opgedeeld (wro.planobject
# draagt de hele vorm) en bevat naast vlakken ook lijnen — voor het
# vereenvoudigen en de sub-pixeltoets maakt dat niet uit: een lijn die binnen
# één pixel past is even onzichtbaar als een vlakje.
BRONNEN: dict[str, dict[str, str]] = {
    "ow": {
        "bron": "p2p.locatie_subdiv",
        "doel": "p2p.locatie_generalisatie",
        "prefix": "idx_locatie_gen",
    },
    "wro": {
        "bron": "wro.planobject",
        "doel": "wro.planobject_generalisatie",
        "prefix": "idx_planobject_gen",
    },
}


# De indexen staan in het DDL-script, maar worden voor een herbouw weggegooid
# en achteraf opnieuw gelegd: een GIST-index bijhouden tijdens miljoenen
# inserts is duurder dan hem in één keer bouwen. Per niveau alleen zijn eigen
# partiele GIST-index, zodat het herbouwen van één niveau de indexen van de
# andere niveaus niet aanraakt.
def _index_ddl(cfg: dict[str, str], niveau: int) -> tuple[str, str]:
    naam = f"{cfg['prefix']}_geom_n{niveau}"
    return naam, (
        f"CREATE INDEX {naam} ON {cfg['doel']} "
        f"USING gist (geometrie) WHERE niveau = {niveau}"
    )


# De koppeling tegel-feature -> object staat los van het niveau en wordt alleen
# herbouwd als alle niveaus opnieuw gevuld worden.
def _id_index(cfg: dict[str, str]) -> tuple[str, str]:
    naam = f"{cfg['prefix']}_id"
    return naam, f"CREATE INDEX {naam} ON {cfg['doel']} (identificatie, niveau)"

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
INSERT INTO {doel} (identificatie, niveau, geometrie, bron_hash)
SELECT identificatie, %(niveau)s, g, bron_hash
  FROM (
    SELECT identificatie,
           ST_SimplifyPreserveTopology(geometrie, %(tol)s) AS g,
           md5(ST_AsBinary(geometrie))::uuid               AS bron_hash
      FROM {bron}
     WHERE ctid >= %(van)s::tid AND ctid < %(tot)s::tid
       AND NOT (ST_XMax(geometrie) - ST_XMin(geometrie) < %(tol)s
            AND ST_YMax(geometrie) - ST_YMin(geometrie) < %(tol)s)
  ) s
 WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
"""


def _paginas(conn, cfg: dict[str, str]) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT relpages FROM pg_class WHERE oid = %s::regclass", (cfg["bron"],))
        return cur.fetchone()["relpages"]


# ── Incrementeel: per bronhouder ─────────────────────────────────────────────
#
# De volledige herbouw is 16 min lokaal (gemeten 2026-08-31) en doet een
# TRUNCATE. Op productie is dat onbruikbaar: de tegellaag is dan leeg zolang de
# herbouw duurt, dus de kaart onder z11 blijft blanco voor bezoekers.
#
# De goedkope weg is niet delta-detectie maar **scoping op bronhouder**, precies
# zoals `refresh-subdiv -b <code>` dat voor de brontabel doet. De sync weet welke
# bronhouders zijn geraakt, en alleen die hoeven opnieuw.
#
# Delta-detectie op `bron_hash` is wél geprobeerd en viel af (gemeten 31-08):
# de bron hashen kost 162 s en de vergelijking per niveau 195 s, dus drie niveaus
# kosten bijna evenveel als de hele herbouw. Bovendien meldde die detectie
# 179.135 wijzigingen op een zojuist herbouwde tabel -- precies het aantal rijen
# dat de sub-pixelzeef weglaat. Zonder diezelfde zeef in de detectiequery
# rapporteert hij elke run hetzelfde spookverschil.
#
# LET OP het prefix-predicaat -- daar zijn twee valkuilen omheen gelopen.
#
# Zonder de text_pattern_ops-index uit
# 2026-09-add-generalisatie-prefix-index.sql kan `identificatie LIKE %s` met een
# *parameter* de gewone btree niet gebruiken: de database draait op en_US.utf8
# en onder een niet-C-collatie kan de planner niet bewijzen dat dit een
# prefix-bereik is. Hij valt dan terug op een volledige parallelle scan --
# kosten 1.645.474 tegen 21.
#
# De voor de hand liggende uitwijk (`>= 'nl.imow-gm0279.' AND
# < 'nl.imow-gm0279/'`) is *fout* onder die collatie: leestekens wegen licht,
# waardoor `nl.imow-gm0279.ambtsgebied...` buiten het bereik valt. Gemeten 0
# rijen waar er 8 zijn -- snel en stil verkeerd, de gevaarlijkste soort.
#
# Mét de index is gewoon `LIKE` weer de beste vorm, en veruit: gemeten 0,035 ms
# tegen 232 ms voor een `COLLATE "C"`-bereik op dezelfde index. Draai die index
# dus voordat je hierop leunt.
BRONHOUDER_SQL = """
INSERT INTO {doel} (identificatie, niveau, geometrie, bron_hash)
SELECT identificatie, %(niveau)s, g, bron_hash
  FROM (
    SELECT identificatie,
           ST_SimplifyPreserveTopology(geometrie, %(tol)s) AS g,
           md5(ST_AsBinary(geometrie))::uuid               AS bron_hash
      FROM {bron}
     WHERE identificatie LIKE %(pat)s
       AND NOT (ST_XMax(geometrie) - ST_XMin(geometrie) < %(tol)s
            AND ST_YMax(geometrie) - ST_YMin(geometrie) < %(tol)s)
  ) s
 WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
"""


def bouw_bronhouders(cfg: dict[str, str], niveaus: dict[int, float],
                     codes: list[str]) -> None:
    """Herbouw de generalisatie voor alleen deze bronhouders. Geen TRUNCATE.

    Per bronhouder en per niveau: eerst de bestaande rijen weg, dan opnieuw
    berekenen. Dat gebeurt in één transactie per bronhouder, zodat een afgebroken
    run geen half gevulde bronhouder achterlaat -- de tegels van de andere
    bronhouders blijven ondertussen gewoon staan.
    """
    conn = get_conn()
    sql = BRONHOUDER_SQL.format(**cfg)
    totaal_rijen, t_totaal = 0, time.monotonic()
    try:
        for code in codes:
            pat = f"nl.imow-{code}.%"
            t0, rijen, weg = time.monotonic(), 0, 0
            with conn.cursor() as cur:
                for n, tol in niveaus.items():
                    cur.execute(
                        f'DELETE FROM {cfg["doel"]} '
                        f'WHERE niveau = %(niveau)s AND identificatie LIKE %(pat)s',
                        {"niveau": n, "pat": pat})
                    weg += cur.rowcount
                    cur.execute(sql, {"niveau": n, "tol": tol, "pat": pat})
                    rijen += cur.rowcount
            conn.commit()
            totaal_rijen += rijen
            console.print(f"  {code:10s} {weg:>8,} weg → {rijen:>8,} nieuw  "
                          f"({time.monotonic() - t0:.1f}s)")
        console.print(f"[bold green]Klaar[/bold green]: {len(codes)} bronhouders, "
                      f"{totaal_rijen:,} rijen in "
                      f"{(time.monotonic() - t_totaal) / 60:.1f} min")
    finally:
        conn.close()


def _bouw_niveau(
    cfg: dict[str, str], niveau: int, tol: float, laatste_blok: int, vanaf: int, workers: int
) -> None:
    sql = INSERT_SQL.format(**cfg)
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
                sql,
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


def _te_bouwen_indexen(
    cfg: dict[str, str], niveaus: dict[int, float], alles: bool
) -> list[tuple[str, str]]:
    lijst = [_index_ddl(cfg, n) for n in niveaus]
    if alles:
        lijst.append(_id_index(cfg))
    return lijst


def _schema(cfg: dict[str, str]) -> str:
    return cfg["doel"].split(".")[0]


def _zonder_indexen(conn, cfg: dict[str, str], indexen: list[tuple[str, str]]) -> None:
    with conn.cursor() as cur:
        for naam, _ in indexen:
            cur.execute(f"DROP INDEX IF EXISTS {_schema(cfg)}.{naam}")
    conn.commit()


def _met_indexen(conn, cfg: dict[str, str], indexen: list[tuple[str, str]]) -> None:
    for naam, ddl in indexen:
        start = time.monotonic()
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {_schema(cfg)}.{naam}")
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
@click.option(
    "--bronhouder",
    multiple=True,
    help="Alleen deze bronhouder(s) herbouwen, bv. --bronhouder gm0995 --bronhouder ws0665. "
         "Geen TRUNCATE: de rest van de tegellaag blijft staan. Dit is de vorm die in "
         "een sync hoort (naast `refresh-subdiv -b`), niet de volledige herbouw.",
)
@click.option("--workers", type=int, default=6, help="Parallelle verbindingen.")
@click.option(
    "--bron",
    type=click.Choice(["ow", "wro"]),
    default="ow",
    help="ow = p2p.locatie_subdiv, wro = wro.planobject.",
)
def main(
    niveau: int | None,
    steekproef: int | None,
    vanaf_blok: int,
    behoud: bool,
    bronhouder: tuple[str, ...],
    workers: int,
    bron: str,
) -> None:
    cfg = BRONNEN[bron]

    if bronhouder:
        # Aparte, veel kortere route: geen ctid-chunking, geen TRUNCATE, geen
        # index-herbouw. De indexen blijven staan omdat het om duizenden rijen
        # gaat en niet om miljoenen -- ze weggooien en opnieuw bouwen zou hier
        # veel duurder zijn dan de inserts zelf.
        niveaus = {niveau: NIVEAUS[niveau]} if niveau else NIVEAUS
        console.print(f"[bold]{cfg['doel']}[/bold] — {len(bronhouder)} bronhouder(s), "
                      f"niveaus {list(niveaus)}")
        bouw_bronhouders(cfg, niveaus, list(bronhouder))
        return

    conn = get_conn()
    try:
        paginas = _paginas(conn, cfg)
        laatste_blok = (steekproef or paginas) - 1
        niveaus = {niveau: NIVEAUS[niveau]} if niveau else NIVEAUS

        console.print(
            f"[bold]{cfg['bron']}[/bold]: {paginas:,} pagina's"
            + (f" — steekproef tot blok {laatste_blok:,}" if steekproef else "")
        )

        alles = niveau is None
        indexen = _te_bouwen_indexen(cfg, niveaus, alles=alles)
        _zonder_indexen(conn, cfg, indexen)

        # Bij een volledige herbouw TRUNCATE in plaats van DELETE: 7 miljoen
        # dode tuples laten de tabel met 2 GB opzwellen tot de autovacuum
        # bijtrekt. Bij een enkel niveau kan dat niet en is DELETE de prijs.
        if alles and not behoud:
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE {cfg['doel']}")
            conn.commit()
            console.print("  tabel geleegd (TRUNCATE)")

        for n, tol in niveaus.items():
            if not behoud and not alles:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {cfg['doel']} WHERE niveau = %s", (n,))
                    console.print(f"  {cur.rowcount:,} bestaande rijen op niveau {n} verwijderd")
                conn.commit()
            _bouw_niveau(cfg, n, tol, laatste_blok, vanaf_blok, workers)

        console.print("\n[bold]Indexen opnieuw bouwen[/bold]")
        _met_indexen(conn, cfg, indexen)

        with conn.cursor() as cur:
            cur.execute(f"ANALYZE {cfg['doel']}")
            cur.execute(
                f"""SELECT niveau, count(*) AS n,
                           sum(ST_NPoints(geometrie)) AS punten
                      FROM {cfg['doel']} GROUP BY niveau ORDER BY niveau"""
            )
            for r in cur.fetchall():
                console.print(
                    f"[bold]niveau {r['niveau']}[/bold]: {r['n']:,} rijen, "
                    f"{r['punten']:,} punten"
                )
            cur.execute(
                f"SELECT pg_size_pretty(pg_total_relation_size('{cfg['doel']}')) AS s"
            )
            console.print(f"[bold]omvang[/bold]: {cur.fetchone()['s']}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
