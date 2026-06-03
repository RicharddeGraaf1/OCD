"""Onderhoud van p2p.locatie_subdiv — de afgeleide, opgedeelde geometrie-tabel.

p2p.locatie_subdiv bevat de geometrie van p2p.locatie, maar dan via
ST_Subdivide(geometrie, 256) opgeknipt in kleine stukjes (één rij per stukje;
identificatie is NIET uniek). Regelingsgebieden zijn grote multipolygons;
st_intersects op de volledige geometrie kost seconden en duwt /v1/adres over
de statement_timeout. Op de opgedeelde stukjes pre-filtert de GiST-index veel
preciezer (~5-90x sneller, identieke resultaatset).

Deze tabel wordt NIET door de gewone loader-INSERT gevuld — hij is afgeleid.
Daarom roept elke OW-load (ZIP via ow_loader, API via api_loader) na afloop
refresh_locatie_subdiv() aan, en is er een CLI-command `refresh-subdiv` voor
een handmatige volledige rebuild.

Alleen Polygon/MultiPolygon-locaties komen in subdiv; punt-/lijn-locaties
hebben geen baat bij subdivision en worden overgeslagen.
"""

from rich.console import Console

from src.db import get_conn

console = Console()

_POLYGON_TYPES = ("ST_Polygon", "ST_MultiPolygon")


def refresh_locatie_subdiv(conn, bronhouder_code: str | None = None,
                           max_vertices: int = 256) -> int:
    """(Her)bouw p2p.locatie_subdiv voor polygon-locaties.

    Verwijdert eerst de bestaande subdiv-rijen van de betrokken locaties en
    bouwt ze opnieuw op. Daardoor is de refresh correct bij zowel een verse
    load (nog geen rijen) als een her-load waarbij geometrieën zijn gewijzigd
    (ON CONFLICT DO UPDATE in de loaders) — de oude subdiv-stukjes worden dan
    netjes vervangen.

    Args:
        conn: open psycopg-connectie (wordt gecommit aan het eind).
        bronhouder_code: beperk tot één bronhouder (de scope van de zojuist
            geladen regelingen, bv. 'gm0344'). None = alle polygon-locaties
            (volledige rebuild).
        max_vertices: max. aantal vertices per stukje (PostGIS-aanbeveling 256).

    Returns:
        Aantal ingevoegde subdiv-rijen (stukjes).
    """
    scope = ""
    params: tuple = ()
    if bronhouder_code:
        scope = "AND l.identificatie LIKE %s"
        params = (f"nl.imow-{bronhouder_code}.%",)

    types_sql = "('" + "','".join(_POLYGON_TYPES) + "')"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM p2p.locatie_subdiv ls
            USING p2p.locatie l
            WHERE ls.identificatie = l.identificatie
              AND ST_GeometryType(l.geometrie) IN {types_sql}
              {scope}
            """,
            params,
        )
        cur.execute(
            f"""
            INSERT INTO p2p.locatie_subdiv (identificatie, geometrie)
            SELECT l.identificatie, ST_Subdivide(l.geometrie, %s)
            FROM p2p.locatie l
            WHERE ST_GeometryType(l.geometrie) IN {types_sql}
              {scope}
            """,
            (max_vertices, *params),
        )
        n = cur.rowcount
    conn.commit()
    return n


def refresh_main(bronhouder_code: str | None = None) -> int:
    """Standalone entry voor de CLI: open eigen connectie en refresh."""
    conn = get_conn()
    try:
        n = refresh_locatie_subdiv(conn, bronhouder_code)
        scope = f"bronhouder {bronhouder_code}" if bronhouder_code else "alle polygon-locaties"
        console.print(f"[green]locatie_subdiv ververst ({scope}): {n} stukjes[/green]")
        return n
    finally:
        conn.close()
