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

import json

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn, normalize_bronhouder_code
from src.loaders.ihr_loader import _ihr_get, load_teksten_for_plan

console = Console()

# PDOK CBS Gebiedsindelingen per jaar; nieuwste eerst zodat een opgeheven gemeente
# zijn meest recente (= meest volledige) grens krijgt. 2013 dekt de meeste
# recente herindelingen, 2009 de wat oudere.
PDOK_HIST_JAREN = [2013, 2009]


def benodigde_opgeheven_codes() -> set[str]:
    """Bronhouder-codes van nog-missende IMRO2006-plannen die geen huidige
    gemeentegrens hebben (= opgeheven gemeenten)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT o.bronhouder_code
                FROM wro.wro_plan_observatie o
                WHERE o.snapshot_id = (SELECT snapshot_id FROM wro.wro_snapshot ORDER BY datum DESC LIMIT 1)
                  AND o.verwijderd_op IS NULL
                  AND o.bronhouder_code IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM wro.ruimtelijk_instrument ri WHERE ri.idn = o.identificatie)
                  AND NOT EXISTS (SELECT 1 FROM core.gemeentegrens g WHERE g.overheidscode = o.bronhouder_code)
            """)
            return {r["bronhouder_code"] for r in cur.fetchall()}
    finally:
        conn.close()


def vul_gemeentegrens_historisch(codes: set[str],
                                 jaren: list[int] | None = None) -> int:
    """Haal grenzen van opgeheven gemeenten uit PDOK CBS Gebiedsindelingen (per
    jaar) en vul core.gemeentegrens_historisch. Neemt de eerste jaar (op volgorde)
    waarin een code voorkomt. Returnt aantal ingevoegde grenzen."""
    jaren = jaren or PDOK_HIST_JAREN
    nodig = {c.upper() for c in codes}
    conn = get_conn()
    ingevoegd = 0
    try:
        for jaar in jaren:
            if not nodig:
                break
            url = (f"https://service.pdok.nl/cbs/gebiedsindelingen/{jaar}/wfs/v1_0"
                   "?service=WFS&version=2.0.0&request=GetFeature"
                   "&typeNames=gebiedsindelingen:gemeente_gegeneraliseerd"
                   "&outputFormat=application/json")
            try:
                data = httpx.get(url, timeout=120).json()
            except Exception as e:
                console.print(f"  [red]{jaar}: WFS-fout {type(e).__name__}[/red]")
                continue
            gevonden = 0
            for f in data.get("features", []):
                code = (f["properties"].get("statcode") or "").upper()
                if code not in nodig:
                    continue
                naam = f["properties"].get("statnaam")
                geom = json.dumps(f.get("geometry"))
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO core.gemeentegrens_historisch (overheidscode, naam, jaar, geometrie)
                        VALUES (lower(%s), %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)))
                        ON CONFLICT (overheidscode) DO NOTHING
                    """, (code, naam, jaar, geom))
                    ingevoegd += cur.rowcount
                nodig.discard(code)
                gevonden += 1
            conn.commit()
            console.print(f"  {jaar}: {gevonden} grenzen gevonden, {len(nodig)} nog nodig")
        if nodig:
            console.print(f"  [yellow]{len(nodig)} niet gevonden: {sorted(nodig)[:8]}[/yellow]")
        return ingevoegd
    finally:
        conn.close()


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
                    # Ambtsgebied = huidige gemeentegrens, of anders de historische
                    # grens van een opgeheven gemeente (prio: huidig vóór historisch).
                    cur.execute("""
                        INSERT INTO wro.ruimtelijk_instrument
                            (idn, dossier, type_plan, naam, planstatus, datum, bronhouder,
                             geometrie, gml_source, pons_status, geometrie_herkomst)
                        SELECT %s, %s, %s, %s, %s, NULL, %s,
                               g.geometrie, 'IHR-IMRO2006', 'actief', 'ambtsgebied-imro2006'
                        FROM (
                            SELECT geometrie, 0 AS prio FROM core.gemeentegrens WHERE overheidscode = %s
                            UNION ALL
                            SELECT geometrie, 1 AS prio FROM core.gemeentegrens_historisch WHERE overheidscode = %s
                            ORDER BY prio LIMIT 1
                        ) g
                        ON CONFLICT (idn) DO NOTHING
                    """, (idn, dossier, type_plan, naam, planstatus, bron, bron, bron))
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
