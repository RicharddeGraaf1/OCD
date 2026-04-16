"""Update waarde_in_regeltekst op bestaande normwaarden.

Haalt per regeling de annotaties op via Presenteren v8 en zet
waarde_in_regeltekst op de bijbehorende normwaarde-rijen in OCD.

Gebruik:
    cd dso-loader
    source .venv/Scripts/activate   (of .venv/bin/activate op Linux)
    python scripts/update_waarde_in_regeltekst.py
"""

import time
from src.db import get_conn
from src.config import cfg
from src.loaders.api_loader import _get
from rich.console import Console

console = Console()


def fetch_normen_from_api(frbr_work: str) -> list[dict]:
    """Haal normen + normwaarden op via regeltekstannotaties.

    De Presenteren v8 API verwacht het work-level URI met slashes
    vervangen door underscores als path parameter.
    """
    encoded = frbr_work.replace("/", "_")
    normen = []

    for endpoint in ("regeltekstannotaties", "divisieannotaties"):
        try:
            url = f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/{endpoint}"
            data = _get(url, params={"locatieSelectie": "primair"})
            if data is None:
                continue

            for norm_key in ("omgevingsnormen", "omgevingswaarden"):
                for norm in data.get(norm_key, []):
                    normen.append(norm)
        except Exception as e:
            console.print(f"  [dim]{endpoint}: {e}[/dim]")

    return normen


def update_regeling(conn, frbr_work: str, naam: str) -> int:
    """Update waarde_in_regeltekst voor alle normwaarden van een regeling."""
    normen = fetch_normen_from_api(frbr_work)
    if not normen:
        console.print(f"  [dim]Geen normen gevonden via API[/dim]")
        return 0

    updated = 0
    with conn.cursor() as cur:
        for norm in normen:
            norm_id = norm.get("identificatie")
            if not norm_id:
                continue
            for nw in norm.get("normwaarden", []):
                wirt_raw = nw.get("waardeInRegeltekst")
                if wirt_raw is None:
                    continue
                # API stuurt soms boolean, soms de string "waarde staat in regeltekst"
                if isinstance(wirt_raw, bool):
                    wirt = wirt_raw
                elif isinstance(wirt_raw, str):
                    wirt = wirt_raw.lower() in ("true", "waarde staat in regeltekst")
                else:
                    continue
                locs = nw.get("locatieRefs", [])
                for loc_id in locs:
                    cur.execute(
                        """UPDATE p2p.normwaarde
                           SET waarde_in_regeltekst = %s
                           WHERE norm_id = %s AND locatie_id = %s
                             AND waarde_in_regeltekst IS NULL""",
                        (wirt, norm_id, loc_id),
                    )
                    updated += cur.rowcount
    conn.commit()
    return updated


def main():
    conn = get_conn()
    try:
        # Vind alle regelingen met normwaarden die nog NULL zijn
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT r.frbr_work, r.bronhouder, b.naam
                FROM p2p.normwaarde nw
                JOIN p2p.norm n ON n.identificatie = nw.norm_id
                JOIN p2p.juridische_regel_norm jrn ON jrn.norm_id = n.identificatie
                JOIN p2p.juridische_regel jr ON jr.identificatie = jrn.juridische_regel_id
                JOIN p2p.tekst_element te ON te.wid = jr.regeltekst_wid
                JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
                JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
                WHERE nw.waarde_in_regeltekst IS NULL
                ORDER BY b.naam
            """)
            regelingen = cur.fetchall()

        console.print(f"[bold]{len(regelingen)} regelingen met onbekende waarde_in_regeltekst[/bold]")

        total_updated = 0
        for i, reg in enumerate(regelingen, 1):
            console.print(f"[{i}/{len(regelingen)}] {reg['naam']} — {reg['frbr_work'][:60]}...")
            try:
                n = update_regeling(conn, reg["frbr_work"], reg["naam"])
                total_updated += n
                if n > 0:
                    console.print(f"  [green]{n} normwaarden bijgewerkt[/green]")
                time.sleep(0.2)  # rate limiting
            except Exception as e:
                console.print(f"  [red]Fout: {e}[/red]")
                conn.rollback()

        console.print(f"\n[bold green]Klaar: {total_updated} normwaarden bijgewerkt[/bold green]")

        # Check remaining NULLs
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM p2p.normwaarde WHERE waarde_in_regeltekst IS NULL")
            remaining = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM p2p.normwaarde WHERE waarde_in_regeltekst IS NOT NULL")
            filled = cur.fetchone()["n"]
        console.print(f"Status: {filled} gevuld, {remaining} nog NULL")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
