"""Laad oude IMRO2006/Artikel-10-plannen die wél in de planvoorraad
(RP-Opvragen) staan maar niet in onze PDOK-geometrie-set.

Deze plannen hebben geen machine-leesbare plangebied-geometrie (ze dateren van
vóór de IMRO2012-GML die PDOK levert, en IHR serveert geen geometrie). We
koppelen ze indicatief aan het **ambtsgebied** (gemeentegrens) van de bronhouder
en taggen `geometrie_herkomst='ambtsgebied-imro2006'`, zodat viewer/bot ze apart
tonen ("gemeentebreed, exacte begrenzing onbekend").

Metadata + teksten uit IHR; geometrie uit core.gemeentegrens.
Work-list = planvoorraad-diff (idns in de laatste snapshot, niet lokaal aanwezig).
"""

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn, normalize_bronhouder_code
from src.loaders.ihr_loader import _ihr_get, load_teksten_for_plan

console = Console()


def _missende_idns(conn, cbs_codes: list[str] | None) -> list[dict]:
    """Idns uit de laatste planvoorraad-snapshot die nog niet in
    ruimtelijk_instrument staan (optioneel per gemeente)."""
    params: list = []
    gm_filter = ""
    if cbs_codes:
        gm_codes = [normalize_bronhouder_code(c) for c in cbs_codes]
        gm_filter = "AND o.bronhouder_code = ANY(%s)"
        params.append(gm_codes)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT o.identificatie, o.bronhouder_code, o.bronhouder_naam,
                   o.planstatus, o.plantype, o.titel
            FROM wro.wro_plan_observatie o
            WHERE o.snapshot_id = (SELECT snapshot_id FROM wro.wro_snapshot ORDER BY datum DESC LIMIT 1)
              AND o.verwijderd_op IS NULL
              {gm_filter}
              AND NOT EXISTS (SELECT 1 FROM wro.ruimtelijk_instrument ri WHERE ri.idn = o.identificatie)
            ORDER BY o.identificatie
        """, params)
        return cur.fetchall()


def load_imro2006_ambtsgebied(cbs_codes: list[str] | None = None) -> int:
    """Laad missende IMRO2006-plannen met ambtsgebied-geometrie + teksten.
    Returnt het aantal geladen instrumenten."""
    if not cfg.IHR_API_KEY:
        console.print("[red]IHR_API_KEY niet gezet in .env[/red]")
        return 0

    conn = get_conn()
    n = 0
    try:
        missend = _missende_idns(conn, cbs_codes)
        console.print(f"[bold]{len(missend)} missende IMRO2006-plannen[/bold]")
        for row in missend:
            idn = row["identificatie"]
            bron = normalize_bronhouder_code(row["bronhouder_code"] or "")
            try:
                meta = _ihr_get(f"/plannen/{idn}")
            except httpx.HTTPError as e:
                console.print(f"  [red]{idn}: IHR-fout {type(e).__name__}[/red]")
                continue
            naam = (meta.get("naam") or row["titel"] or "onbekend")[:500]
            type_plan = row["plantype"] or "bestemmingsplan"
            planstatus = row["planstatus"] or "onbekend"
            dossier = idn.rsplit("-", 1)[0] if "-" in idn else idn

            naam_overheid = row["bronhouder_naam"] or bron
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO core.planstatus (code) VALUES (%s) ON CONFLICT DO NOTHING",
                                (planstatus,))
                    # Manifest is nodig want wro_dossier.manifest_code -> wro_manifest (NOT NULL FK).
                    cur.execute("INSERT INTO wro.wro_manifest (overheidscode, naam_overheid) "
                                "VALUES (%s, %s) ON CONFLICT DO NOTHING", (bron, naam_overheid))
                    cur.execute("INSERT INTO wro.wro_dossier (dossiernummer, manifest_code, status) "
                                "VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING", (dossier, bron))
                    # Geometrie = ambtsgebied van de bronhouder. INSERT..SELECT: als er
                    # geen gemeentegrens is (opgeheven gemeente) wordt niets ingevoegd.
                    cur.execute("""
                        INSERT INTO wro.ruimtelijk_instrument
                            (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                             geometrie, gml_source, pons_status, geometrie_herkomst)
                        SELECT %s, %s, %s, %s, %s, NULL, %s,
                               g.geometrie, 'IHR-IMRO2006', 'actief', 'ambtsgebied-imro2006'
                        FROM core.gemeentegrens g WHERE g.overheidscode = %s
                        ON CONFLICT (idn) DO NOTHING
                    """, (idn, dossier, type_plan, naam, planstatus, bron, bron))
                    geladen = cur.rowcount
                conn.commit()

                if geladen == 0:
                    console.print(f"  [yellow]{idn}: geen ambtsgebied voor {bron} — overgeslagen[/yellow]")
                    continue
                n_tekst = load_teksten_for_plan(conn, idn)
                console.print(f"  [green]{idn}[/green] ({naam[:40]}) — ambtsgebied {bron}, {n_tekst} teksten")
                n += 1
            except Exception as e:
                conn.rollback()  # één slecht plan mag de rest niet meeslepen
                console.print(f"  [red]{idn}: {type(e).__name__} — overgeslagen[/red]")
                continue
        return n
    finally:
        conn.close()
