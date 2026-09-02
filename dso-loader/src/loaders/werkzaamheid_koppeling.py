"""Laadt de M:N-relatie werkzaamheid <-> activiteit uit de RTR.

Vervangt de kapotte koppeling in `imtr_loader._load_werkzaamheden`, die per
werkzaamheid één willekeurige activiteit bewaarde (zie gaps#G-136 en
scripts/2026-09-add-werkzaamheid-activiteit-junctie.sql).

Omvang: 293 werkzaamheden, mediaan ~356 koppelingen elk (min 11, max 722), dus
~104.000 rijen uit ~650 calls op pageSize=200.

Conservatief: gebruikt `src.beleefde_client.Beleefd` — serieel, 1 req/s, en
stopt bij herhaalde 503 in plaats van door te duwen. Elke afgeronde
werkzaamheid wordt gecheckpoint in `i2a.werkzaamheid_koppel_run`, zodat
afbreken niets kost en een volgende run verdergaat waar deze stopte.
"""

from __future__ import annotations

import datetime
import re

from rich.console import Console

from src.beleefde_client import Beleefd, DienstWijktAf
from src.config import cfg
from src.db import get_conn

console = Console()

PAGE = 200
_NS = re.compile(r"^nl\.imow-([a-z]+)([0-9a-z]*)\.")


def _laag(urn: str) -> tuple[str | None, str | None]:
    """('gemeente', 'gm0344') uit een activiteit-URN. De RTR geeft geen
    bevoegd gezag mee op het koppeling-object — alleen `urn` en `_links`."""
    m = _NS.match(urn or "")
    if not m:
        return None, None
    pref = m.group(1)
    return {
        "gm": "gemeente", "pv": "provincie",
        "ws": "waterschap", "mnre": "rijk",
    }.get(pref), f"{pref}{m.group(2)}"


def _items(payload: dict, *sleutels: str) -> list[dict]:
    """De RTR wisselt van sleutelnaam tussen endpoints en zelfs tussen
    pagina's; accepteer alle bekende varianten."""
    emb = payload.get("_embedded", {}) or {}
    for s in sleutels:
        if emb.get(s):
            return emb[s]
    return []


def _alle_werkzaamheden(c: Beleefd) -> list[dict]:
    uit: list[dict] = []
    page = 1
    while True:
        r = c.get(f"{cfg.RTR_BASE}/werkzaamheden",
                  params={"pageSize": PAGE, "page": page})
        r.raise_for_status()
        j = r.json()
        items = _items(j, "werkzaamheden")
        if not items:
            break
        uit.extend(items)
        if not (j.get("_links", {}) or {}).get("next"):
            break
        page += 1
    return uit


def _koppelingen(c: Beleefd, urn: str, datum: str) -> tuple[list[str], int]:
    uit: list[str] = []
    page = 1
    while True:
        r = c.get(f"{cfg.RTR_BASE}/werkzaamheden/{urn}/activiteitKoppelingen",
                  params={"datum": datum, "pageSize": PAGE, "page": page})
        if r.status_code == 404:
            break
        r.raise_for_status()
        j = r.json()
        items = _items(j, "activiteitKoppelingen")
        if not items:
            break
        uit.extend(i["urn"] for i in items if i.get("urn"))
        if not (j.get("_links", {}) or {}).get("next"):
            break
        page += 1
    return uit, page


def laad(tempo: float = 1.0, alleen_s_nachts: bool = False,
         opnieuw: bool = False, limit: int | None = None) -> dict:
    """Haal alle koppelingen op en schrijf ze weg.

    opnieuw=True negeert de checkpoints en haalt alles opnieuw op.
    """
    datum = datetime.date.today().strftime("%Y-%m-%d")
    conn = get_conn()
    stats = {"werkzaamheden": 0, "overgeslagen": 0, "koppelingen": 0, "calls": 0}
    try:
        with Beleefd(tempo=tempo, alleen_s_nachts=alleen_s_nachts) as c:
            console.print(f"  Werkzaamheden ophalen (tempo {tempo:g}/s)...")
            alle = _alle_werkzaamheden(c)
            console.print(f"  [green]{len(alle)} werkzaamheden[/green]")

            with conn.cursor() as cur:
                for w in alle:
                    # trefwoorden = de synoniemenlijst waarop een
                    # initiatiefnemer zoekt; komt gratis mee in deze lijst-call.
                    cur.execute(
                        """INSERT INTO i2a.werkzaamheid (urn, naam, trefwoorden)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (urn) DO UPDATE
                             SET naam        = EXCLUDED.naam,
                                 trefwoorden = EXCLUDED.trefwoorden""",
                        (w["urn"], w.get("omschrijving", w["urn"]),
                         w.get("trefwoorden") or None))
                cur.execute("SELECT werkzaamheid_urn FROM i2a.werkzaamheid_koppel_run")
                gedaan = {r["werkzaamheid_urn"] for r in cur.fetchall()}
            conn.commit()

            todo = [w for w in alle if opnieuw or w["urn"] not in gedaan]
            stats["overgeslagen"] = len(alle) - len(todo)
            if limit:
                todo = todo[:limit]
            console.print(f"  {len(todo)} te doen, {stats['overgeslagen']} al gecheckpoint")

            for i, w in enumerate(todo, 1):
                urn = w["urn"]
                urns, paginas = _koppelingen(c, urn, datum)
                with conn.cursor() as cur:
                    if opnieuw:
                        cur.execute("DELETE FROM i2a.werkzaamheid_activiteit "
                                    "WHERE werkzaamheid_urn = %s", (urn,))
                    for a in urns:
                        laag, ns = _laag(a)
                        cur.execute(
                            """INSERT INTO i2a.werkzaamheid_activiteit
                                   (werkzaamheid_urn, activiteit_urn,
                                    gezien_in_p2p, bestuurslaag, overheid_ns)
                               VALUES (%s, %s,
                                    EXISTS (SELECT 1 FROM p2p.activiteit
                                            WHERE identificatie = %s), %s, %s)
                               ON CONFLICT (werkzaamheid_urn, activiteit_urn)
                               DO UPDATE SET gezien_in_p2p = EXCLUDED.gezien_in_p2p,
                                             bestuurslaag  = EXCLUDED.bestuurslaag,
                                             overheid_ns   = EXCLUDED.overheid_ns""",
                            (urn, a, a, laag, ns))
                    # Checkpoint pas ná het wegschrijven: een afgebroken
                    # werkzaamheid wordt de volgende run gewoon opnieuw gedaan.
                    cur.execute(
                        """INSERT INTO i2a.werkzaamheid_koppel_run
                               (werkzaamheid_urn, koppelingen, paginas)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (werkzaamheid_urn) DO UPDATE
                             SET koppelingen = EXCLUDED.koppelingen,
                                 paginas     = EXCLUDED.paginas,
                                 afgerond_op = now()""",
                        (urn, len(urns), paginas))
                conn.commit()
                stats["werkzaamheden"] += 1
                stats["koppelingen"] += len(urns)
                if i % 25 == 0 or i == len(todo):
                    console.print(f"    {i}/{len(todo)} — {stats['koppelingen']} "
                                  f"koppelingen, {c.calls} calls")
            stats["calls"] = c.calls
    except DienstWijktAf as e:
        conn.commit()
        console.print(f"  [yellow]Gestopt: {e}[/yellow]")
        console.print("  [yellow]Het werk tot hier is gecheckpoint; draai het "
                      "commando later opnieuw om verder te gaan.[/yellow]")
    finally:
        conn.close()

    console.print(f"  [green]{stats['werkzaamheden']} werkzaamheden, "
                  f"{stats['koppelingen']} koppelingen, {stats['calls']} calls[/green]")
    return stats
