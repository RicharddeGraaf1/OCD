"""Snapshot de IMRO-plannenvoorraad via RP-Opvragen API v4.

Meet de leegloop van de bestemmingsplan-voorraad (IMRO-kant) als tegenhanger van
de pons-aangroei (Ow-kant). Zie vault analysis/RP-planvoorraad.md.

Mechaniek:
- Bron = RP-Opvragen v4 `/plannen?planType=bestemmingsplan`. De listing-payload
  bevat al `verwijderdOp`, `relatiesMetExternePlannen` en `dossier`, dus één trek
  vangt alles — geen per-plan detail-calls.
- Er is GEEN simpele bronhouder-queryfilter (parameters worden stil genegeerd), dus
  we doen een nationale trek en groeperen zelf op bronhoudercode.
- HAL cursor-paginering via `_links.next`.
- Weg-signaal = `verwijderdOp` (autoritatief, mét datum). De presence-tijdlijn volgt
  uit het voorkomen van een identificatie over opeenvolgende snapshots.

Vereiste headers: Accept: application/hal+json + Accept-Crs/Content-Crs (zonder → 406).
"""

import datetime as dt
import json
from collections import Counter

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.rate_limiter import limiter

console = Console()

HEADERS = {
    "X-Api-Key": cfg.IHR_API_KEY,
    "Accept": "application/hal+json",
    "Accept-Crs": "epsg:28992",
    "Content-Crs": "epsg:28992",
}


def _norm_bronhouder(overheid: dict | None) -> str | None:
    """Gemeentecode '0828' → 'gm0828' (joinbaar op ruimtelijk_instrument.bronhouder).

    Alleen gemeenten krijgen de gm-prefix; provincie-/waterschapscodes blijven
    ongewijzigd (een provincie kan ook een 4-cijferige code hebben).
    """
    overheid = overheid or {}
    code = overheid.get("code")
    typ = (overheid.get("type") or "").lower()
    if code and "gemeente" in typ and len(code) == 4 and code.isdigit():
        return f"gm{code}"
    return code


def _parse_plan(p: dict) -> dict:
    overheid = p.get("beleidsmatigVerantwoordelijkeOverheid") or {}
    psi = p.get("planstatusInfo") or {}
    dossier = p.get("dossier") or {}
    rel = p.get("relatiesMetExternePlannen") or {}
    rel_clean = {k: v for k, v in rel.items() if v}  # alleen niet-lege relaties
    return {
        "identificatie": p.get("id"),
        "dossier": dossier.get("id"),
        "bronhouder_code": _norm_bronhouder(overheid),
        "bronhouder_naam": overheid.get("naam"),
        "titel": p.get("naam"),
        "plantype": p.get("type"),
        "planstatus": psi.get("planstatus"),
        "planstatus_datum": psi.get("datum") or None,
        "dossierstatus": dossier.get("status"),
        "is_tam": bool(p.get("isTamPlan")),
        "is_paraplu": bool(p.get("isParapluplan")),
        "verwijderd_op": p.get("verwijderdOp"),
        "einde_rechtsgeldigheid": p.get("eindeRechtsgeldigheid"),
        "relaties": json.dumps(rel_clean) if rel_clean else None,
    }


INSERT_SQL = """
    INSERT INTO wro.wro_plan_observatie
        (snapshot_id, identificatie, dossier, bronhouder_code, bronhouder_naam,
         titel, plantype, planstatus, planstatus_datum, dossierstatus,
         is_tam, is_paraplu, verwijderd_op, einde_rechtsgeldigheid, relaties)
    VALUES
        (%(snapshot_id)s, %(identificatie)s, %(dossier)s, %(bronhouder_code)s, %(bronhouder_naam)s,
         %(titel)s, %(plantype)s, %(planstatus)s, %(planstatus_datum)s, %(dossierstatus)s,
         %(is_tam)s, %(is_paraplu)s, %(verwijderd_op)s, %(einde_rechtsgeldigheid)s, %(relaties)s)
    ON CONFLICT (snapshot_id, identificatie) DO NOTHING
"""


def load_planvoorraad_snapshot(datum: str | None = None,
                               plan_type: str = "bestemmingsplan",
                               page_size: int = 100,
                               max_pages: int | None = None) -> int:
    """Trek één snapshot van de plannenvoorraad en schrijf naar wro.*.

    Retourneert het snapshot_id. Idempotent per (datum, bron): een herhaalde run
    op dezelfde datum hergebruikt het snapshot en herlaadt de observaties.
    """
    if not cfg.IHR_API_KEY:
        console.print("[red]IHR_API_KEY niet gezet in .env[/red]")
        raise SystemExit(1)

    base = cfg.IHR_BASE.rstrip("/")
    datum = datum or dt.date.today().isoformat()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO wro.wro_snapshot (datum, bron)
                   VALUES (%s, 'rp-opvragen-v4')
                   ON CONFLICT (datum, bron)
                   DO UPDATE SET aangemaakt_op = NOW()
                   RETURNING snapshot_id""",
                (datum,),
            )
            snapshot_id = cur.fetchone()["snapshot_id"]
            cur.execute(
                "DELETE FROM wro.wro_plan_observatie WHERE snapshot_id = %s",
                (snapshot_id,),
            )
        conn.commit()

        console.print(f"[bold]Snapshot {snapshot_id}[/bold] ({datum}) — trek "
                      f"planType={plan_type}, pageSize={page_size}")

        url = f"{base}/plannen"
        params = {"planType": plan_type, "pageSize": page_size, "page": 1}
        total = tam = verwijderd = pages = 0
        per_bron: Counter = Counter()

        while url and (max_pages is None or pages < max_pages):
            with limiter:
                r = httpx.get(url, headers=HEADERS,
                              params=params if pages == 0 else None, timeout=60)
            r.raise_for_status()
            d = r.json()
            plannen = d.get("_embedded", {}).get("plannen", []) or []

            rows = []
            for p in plannen:
                rec = _parse_plan(p)
                if not rec["identificatie"]:
                    continue
                rec["snapshot_id"] = snapshot_id
                rows.append(rec)
                total += 1
                if rec["is_tam"]:
                    tam += 1
                if rec["verwijderd_op"]:
                    verwijderd += 1
                if rec["bronhouder_code"]:
                    per_bron[rec["bronhouder_code"]] += 1

            if rows:
                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, rows)
                conn.commit()

            pages += 1
            if pages % 25 == 0:
                console.print(f"  pagina {pages}: {total} plannen "
                              f"({verwijderd} verwijderd, {tam} TAM)")

            nxt = (d.get("_links") or {}).get("next") or {}
            url = nxt.get("href") if isinstance(nxt, dict) else None
            if not plannen:
                break

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wro.wro_snapshot SET aantal_plannen = %s WHERE snapshot_id = %s",
                (total, snapshot_id),
            )
        conn.commit()

        legacy = total - tam
        console.print(
            f"[green]Klaar: snapshot {snapshot_id} ({datum})[/green]\n"
            f"  {total} bestemmingsplannen · {tam} TAM · {legacy} legacy (niet-TAM)\n"
            f"  {verwijderd} met verwijderdOp · {len(per_bron)} bronhouders · {pages} pagina's"
        )
        return snapshot_id
    finally:
        conn.close()
