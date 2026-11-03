"""De echte plangrens van een Wro-plan, uit de IHR (G-177).

Plannen die niet in de PDOK-set staan kregen tot 2026-11-03 het ambtsgebied als geometrie
(`geometrie_herkomst='ambtsgebied-imro2006'`), omdat de IHR geen geometrie zou serveren. Dat
doet hij wel: `GET /plannen/{id}?expand=geometrie`. Door die noodgrens verscheen zo'n plan op
elk adres in de gemeente.

`neem_plangrenzen_over` werkt de bestaande plannen bij; `wro_imro2006` gebruikt dezelfde
functies voor nieuwe plannen. Heeft de IHR geen grens, dan blijft het ambtsgebied staan.
"""

import json
import time

import httpx
from rich.console import Console

from src.beleefde_client import in_voorkeursvenster, user_agent
from src.db import get_conn, normalize_bronhouder_code
from src.loaders.ihr_loader import _ihr_get

console = Console()

# Bulkwerk: serieel, 1 per seconde, en alleen in het voorkeursvenster van het stelsel
# (22:00-06:00, docs/dso-llms.txt). De run van 2026-11-03 begon overdag; daarom afgedwongen.
PAUZE_S = 1.0
MAX_FOUTEN_OP_RIJ = 3

HERKOMST_AMBTSGEBIED = "ambtsgebied-imro2006"
HERKOMST_IHR = "ihr-plangrens"
GML_BRON_IHR = "IHR-plangrens"

# GeoJSON uit de IHR naar een geldige vlak-geometrie in RD. ST_CollectionExtract(.., 3): een
# reparatie kan losse lijnen of punten opleveren, en die horen niet bij de plangrens.
GEOMETRIE_SQL = """ST_CollectionExtract(ST_MakeValid(
                       ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)), 3)"""

POGINGEN = 5


def plan_met_grens(idn: str) -> dict | None:
    """Het plan uit de IHR, met `geometrie` in RD. None als de IHR het plan niet kent."""
    for poging in range(POGINGEN):
        try:
            return _ihr_get(f"/plannen/{idn}", params={"expand": "geometrie"},
                            headers={"Accept-Crs": "epsg:28992", "User-Agent": user_agent()})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            if e.response.status_code != 429 and e.response.status_code < 500:
                raise
            fout = e
        except httpx.TransportError as e:
            fout = e
        time.sleep(5 * (poging + 1))
    raise fout


def grens_als_json(plan: dict | None) -> str | None:
    """De geometrie van een IHR-plan als GeoJSON-tekst, of None als die er niet is."""
    grens = (plan or {}).get("geometrie")
    return json.dumps(grens) if grens and grens.get("coordinates") else None


def neem_plangrenzen_over(cbs_codes: list[str] | None = None, droog: bool = False) -> dict:
    """Vervang het ambtsgebied door de IHR-grens. Eén commit per plan: een afgebroken run
    laat bijgewerkte plannen staan en pakt de volgende keer alleen de rest.

    Weigert te starten buiten het voorkeursvenster en stopt zodra dat sluit, of na
    MAX_FOUTEN_OP_RIJ fouten achter elkaar: dan hoort de run te wijken, niet door te duwen."""
    tel = {"bijgewerkt": 0, "geen_grens": 0, "niet_in_ihr": 0, "fout": 0}
    if not in_voorkeursvenster():
        raise RuntimeError("Buiten het voorkeursvenster 22:00-06:00: niet gestart.")
    fouten_op_rij = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT idn FROM wro.ruimtelijk_instrument
                   WHERE geometrie_herkomst = %s
                     AND (%s::text[] IS NULL OR bronhouder = ANY(%s))
                   ORDER BY idn""",
                (HERKOMST_AMBTSGEBIED,
                 [normalize_bronhouder_code(c) for c in cbs_codes] if cbs_codes else None,
                 [normalize_bronhouder_code(c) for c in cbs_codes] if cbs_codes else None),
            )
            idns = [r["idn"] for r in cur.fetchall()]
        conn.commit()
        console.print(f"[bold]{len(idns)} plannen met het ambtsgebied als grens[/bold]")

        for i, idn in enumerate(idns, 1):
            if not in_voorkeursvenster():
                console.print(f"[yellow]Venster gesloten, gestopt na {i - 1} van {len(idns)} plannen.[/yellow]")
                break
            if fouten_op_rij >= MAX_FOUTEN_OP_RIJ:
                console.print(f"[red]{fouten_op_rij} fouten op rij, gestopt na {i - 1} van {len(idns)} plannen.[/red]")
                break
            if i > 1:
                time.sleep(PAUZE_S)
            try:
                plan = plan_met_grens(idn)
                grens = grens_als_json(plan)
                if plan is None:
                    tel["niet_in_ihr"] += 1
                    console.print(f"  [yellow]{idn}: niet in de IHR, ambtsgebied blijft[/yellow]")
                elif grens is None:
                    tel["geen_grens"] += 1
                    console.print(f"  [yellow]{idn}: geen grens in de IHR, ambtsgebied blijft[/yellow]")
                elif not droog:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""UPDATE wro.ruimtelijk_instrument
                                SET geometrie = {GEOMETRIE_SQL},
                                    geometrie_herkomst = %s, gml_source = %s, laatst_geladen = NOW()
                                WHERE idn = %s AND geometrie_herkomst = %s""",
                            (grens, HERKOMST_IHR, GML_BRON_IHR, idn, HERKOMST_AMBTSGEBIED),
                        )
                    conn.commit()
                    tel["bijgewerkt"] += 1
                else:
                    tel["bijgewerkt"] += 1
                fouten_op_rij = 0
            except Exception as e:
                conn.rollback()  # één slecht plan mag de rest niet meeslepen
                tel["fout"] += 1
                fouten_op_rij += 1
                console.print(f"  [red]{idn}: {type(e).__name__} — overgeslagen[/red]")
            if i % 100 == 0:
                console.print(f"  {i}/{len(idns)}: {tel}")
    finally:
        conn.close()
    return tel
