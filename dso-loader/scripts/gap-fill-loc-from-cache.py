"""Gerichte gap-fill van activiteit_locatieaanduiding uit cache.

Doel: voor jr-records die wel bestaan maar geen ALA-rijen hebben (gevolg van
pre-2026-06-09 loader-bug), parse alleen de regels*.xml uit de cached ZIP en
schrijf de ALA-rijen. Sla het hele tekst/geo/normwaarde-parsing-pad over —
die data is al in de DB en het hercomputen is duur (ST_Union op
locatiegroep_lid is minuten per ZIP).

Strategie:
1. Vind alle gap-jr's (jr-zonder-loc, regeling_expression bekend)
2. Groepeer per regeling_expression → mapt naar cached ZIP
3. Per ZIP: lees alleen de 3 regel-XML's, parse via fixed parser,
   INSERT alleen de ALA-rijen voor de jr's die we kennen
4. Geen tekst, geen geo, geen norm.

Geen DSO downloads — werkt 100% offline.
"""
import sys
import time
import zipfile
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

from src.db import get_conn
from src.parsers.ow_xml import parse_juridische_regels

CACHE = Path("c:/GIT/OCD/dso-loader/data/downloads/ow")
REGEL_FILES = (
    "OW-bestanden/regelsvooriedereen.xml",
    "OW-bestanden/instructieregels.xml",
    "OW-bestanden/omgevingswaarderegels.xml",
)
console = Console()


def expr_to_zip(expr: str) -> str:
    """/akn/nl/act/pv23/2023/12_33_gm0141/nld@50 -> akn_nl_act_pv23_2023_12_33_gm0141.zip"""
    return expr.split("/nld@")[0].replace("/", "_").strip("_") + ".zip"


def main():
    cached = {z.name for z in CACHE.iterdir() if z.suffix == ".zip"}
    console.print(f"Cached ZIPs: {len(cached)}")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("""
            SELECT jr.identificatie, jr.regeling_expression
            FROM p2p.juridische_regel jr
            LEFT JOIN p2p.activiteit_locatieaanduiding ala
                ON ala.juridische_regel_id = jr.identificatie
            WHERE ala.juridische_regel_id IS NULL
              AND jr.regeling_expression IS NOT NULL
        """)
        rows = cur.fetchall()

    by_expr = defaultdict(set)
    for r in rows:
        by_expr[r["regeling_expression"]].add(r["identificatie"])

    todo, skipped = [], 0
    for expr, ids in by_expr.items():
        z = expr_to_zip(expr)
        if z in cached:
            todo.append((expr, z, ids))
        else:
            skipped += 1

    console.print(f"Gap-regelingen: {len(by_expr)}, te fillen: {len(todo)}, niet in cache: {skipped}")
    total_jrs = sum(len(ids) for _, _, ids in todo)
    console.print(f"Totaal jr-zonder-loc te fillen: {total_jrs:,}")

    succ_jrs = 0
    fail_zips = 0
    start = time.monotonic()

    with get_conn() as conn, conn.cursor() as cur:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} ZIPs"),
            TextColumn("|"),
            TextColumn("{task.fields[jrs]:,} jr's"),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("Gap-fill", total=len(todo), jrs=0)
            for expr, zip_name, gap_ids in todo:
                zip_path = CACHE / zip_name
                try:
                    with zipfile.ZipFile(zip_path) as z:
                        for rf in REGEL_FILES:
                            if rf not in z.namelist():
                                continue
                            regels = parse_juridische_regels(z.read(rf))
                            for regel in regels:
                                if regel["identificatie"] not in gap_ids:
                                    continue
                                # ALAs (activiteit-gebonden)
                                alas = regel.get("activiteit_locatie_aanduidingen", [])
                                inserted = False
                                for ala in alas:
                                    cur.execute(
                                        """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                                           VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                                           ON CONFLICT DO NOTHING""",
                                        (ala["locatie_id"],),
                                    )
                                    cur.execute(
                                        """INSERT INTO p2p.activiteit (identificatie, naam, is_tophaak)
                                           VALUES (%s, '', false) ON CONFLICT DO NOTHING""",
                                        (ala["activiteit_id"],),
                                    )
                                    try:
                                        cur.execute(
                                            """INSERT INTO p2p.activiteit_locatieaanduiding
                                               (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                                               VALUES (%s, %s, %s, %s)""",
                                            (regel["identificatie"], ala["activiteit_id"],
                                             ala["locatie_id"], ala["kwalificatie"]),
                                        )
                                        inserted = True
                                    except Exception:
                                        conn.rollback()
                                # Fallback: directe locatieRefs
                                if not alas:
                                    for loc_ref in regel.get("direct_locatie_refs", []):
                                        cur.execute(
                                            """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                                               VALUES (%s, 'Gebied', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                                               ON CONFLICT DO NOTHING""",
                                            (loc_ref,),
                                        )
                                        try:
                                            cur.execute(
                                                """INSERT INTO p2p.activiteit_locatieaanduiding
                                                   (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                                                   VALUES (%s, NULL, %s, NULL)""",
                                                (regel["identificatie"], loc_ref),
                                            )
                                            inserted = True
                                        except Exception:
                                            conn.rollback()
                                if inserted:
                                    succ_jrs += 1
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    fail_zips += 1
                    console.print(f"[red]FAIL {zip_name}: {type(e).__name__}: {str(e)[:100]}[/red]")
                progress.update(task, advance=1, jrs=succ_jrs)

    elapsed = time.monotonic() - start
    console.print(f"\n[bold green]Klaar: {succ_jrs:,} jr-rijen gevuld, "
                  f"{fail_zips} zip-fails, {skipped} skipped, {elapsed:.0f}s[/bold green]")


if __name__ == "__main__":
    main()
