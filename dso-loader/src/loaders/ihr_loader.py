"""Load Wro planteksten via IHR (Informatiehuis Ruimte) API v4.

Fetches text blocks per bestemmingsplan and stores them as wro_tekst_object.
"""

import re

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.rate_limiter import limiter

console = Console()


def _ihr_get(path: str, params: dict | None = None) -> dict:
    headers = {"X-Api-Key": cfg.IHR_API_KEY}
    with limiter:
        resp = httpx.get(f"{cfg.IHR_BASE}{path}", headers=headers,
                         params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _strip_xml(text: str) -> str:
    """Strip XML/HTML tags from text content."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def _determine_niveau(kruimelpad: list) -> int:
    """Determine nesting level from kruimelpad breadcrumb."""
    return len(kruimelpad)


def _determine_object_type(titel: str) -> str:
    """Guess object type from titel."""
    t = titel.lower().strip()
    if t.startswith("hoofdstuk"):
        return "Hoofdstuk"
    if t.startswith("afdeling"):
        return "Afdeling"
    if t.startswith("paragraaf") or t.startswith("§"):
        return "Paragraaf"
    if t.startswith("artikel") or re.match(r"^\d+\.\d+", t):
        return "Artikel"
    if t.startswith("bijlage"):
        return "Bijlage"
    if t.startswith("toelichting"):
        return "Toelichting"
    if t.startswith("regels"):
        return "Regels"
    return "Overig"


def load_teksten_for_plan(conn, plan_idn: str) -> int:
    """Load all teksten for a single plan via IHR API."""
    all_teksten = []
    page = 1
    while True:
        try:
            data = _ihr_get(f"/plannen/{plan_idn}/teksten",
                            {"pageSize": 100, "page": page})
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
                return 0
            raise
        items = data.get("_embedded", {}).get("teksten", [])
        all_teksten.extend(items)
        total = data.get("page", {}).get("totalElements", 0)
        if not items or len(all_teksten) >= total:
            break
        page += 1

    if not all_teksten:
        return 0

    id_map = {}
    with conn.cursor() as cur:
        for t in all_teksten:
            tekst_id = t.get("id", "")
            if not tekst_id:
                continue

            titel = t.get("titel", "")
            inhoud_raw = t.get("inhoud") or ""
            inhoud = _strip_xml(inhoud_raw) if inhoud_raw else None
            volgnummer = t.get("volgnummer", 0)
            kruimelpad = t.get("kruimelpad", [])
            niveau = _determine_niveau(kruimelpad)
            object_type = _determine_object_type(titel)

            parent_id = None
            if kruimelpad:
                parent_id = kruimelpad[-1].get("id")

            nummer_match = re.match(r"(?:Artikel\s+)?(\d+(?:\.\d+)*)", titel)
            nummer = nummer_match.group(1) if nummer_match else None
            naam = titel

            cur.execute(
                """INSERT INTO wro.wro_tekst_object
                   (identificatie, instrument_idn, volgnummer, niveau, parent_id,
                    object_type, label, nummer, naam, inhoud)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (tekst_id, plan_idn, volgnummer,
                 min(niveau, 11), parent_id,
                 object_type, titel[:200] if titel else None,
                 nummer, naam[:200] if naam else None,
                 inhoud),
            )

    conn.commit()
    return len(all_teksten)


def load_wro_teksten(cbs_codes: list[str] | None = None):
    """Load Wro planteksten for all loaded instruments."""
    if not cfg.IHR_API_KEY:
        console.print("[red]IHR_API_KEY not set in .env[/red]")
        return

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if cbs_codes:
                from src.db import normalize_bronhouder_code
                gm_codes = [normalize_bronhouder_code(c) for c in cbs_codes]
                cur.execute(
                    "SELECT idn, naam FROM wro.ruimtelijk_instrument WHERE bronhouder = ANY(%s) ORDER BY idn",
                    (gm_codes,))
            else:
                cur.execute("SELECT idn, naam FROM wro.ruimtelijk_instrument ORDER BY idn")
            instruments = cur.fetchall()

        console.print(f"Loading teksten for {len(instruments)} instruments...")

        total_teksten = 0
        loaded = 0
        failed = 0
        for i, inst in enumerate(instruments):
            idn = inst["idn"]
            try:
                n = load_teksten_for_plan(conn, idn)
                if n > 0:
                    total_teksten += n
                    loaded += 1
                if (i + 1) % 100 == 0:
                    console.print(f"  {i+1}/{len(instruments)}: {loaded} plans with teksten, {total_teksten} total")
            except Exception as e:
                failed += 1
                if failed <= 5:
                    console.print(f"  [dim]Failed {idn}: {str(e)[:60]}[/dim]")

        console.print(f"[green]Done: {loaded} plans, {total_teksten} teksten loaded ({failed} failed)[/green]")
    finally:
        conn.close()
