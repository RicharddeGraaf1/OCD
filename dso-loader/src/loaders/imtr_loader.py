"""Load IMTR content: RTR activiteiten + STTR regelbestanden.

Two APIs:
1. RTR v2: GET /activiteiten — list activities with metadata
2. STTR v1: GET /toepasbareRegels — list regelbestanden
              GET /toepasbareRegels/{id}/sttrBestand — download DMN XML
"""

from lxml import etree

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.rate_limiter import limiter

console = Console()

# Namespaces in DMN XML
DMN_NS = {
    "semantic": "http://www.omg.org/spec/DMN/20151101/dmn.xsd",
    "uitv": "http://toepasbare-regels.omgevingswet.overheid.nl/v1.0/Uitvoeringsregel",
    "inter": "http://toepasbare-regels.omgevingswet.overheid.nl/v1.0/Interactieregel",
    "bedr": "http://toepasbare-regels.omgevingswet.overheid.nl/v1.0/Bedrijfsregel",
    "content": "http://toepasbare-regels.omgevingswet.overheid.nl/v1.0/Content",
}


def _api_get(base_url: str, path: str, params: dict | None = None) -> dict:
    """GET request to a DSO API with shared rate limiting."""
    url = f"{base_url}{path}"
    headers = {"x-api-key": cfg.DSO_API_KEY}
    with limiter:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _api_post(base_url: str, path: str, json_body: dict) -> dict:
    """POST request to a DSO API with shared rate limiting."""
    url = f"{base_url}{path}"
    headers = {"x-api-key": cfg.DSO_API_KEY, "Content-Type": "application/json"}
    with limiter:
        resp = httpx.post(url, headers=headers, json=json_body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _load_rtr_activiteiten(conn, organisatie_code: str, naam: str) -> int:
    """Load RTR activiteiten for a bestuursorgaan via organisatieCode."""
    console.print(f"  Loading RTR activiteiten for {naam}...")

    all_acts = []
    page = 1
    while True:
        data = _api_post(cfg.RTR_BASE, "/activiteiten/_zoek", {
            "datum": "10-04-2026",
            "bestuursorgaan": {"organisatieCode": organisatie_code},
            "pageSize": 200,
            "page": page,
        })
        items = data.get("_embedded", {}).get("activiteiten", [])
        all_acts.extend(items)
        total = data.get("page", {}).get("totalElements", 0)
        if not items or len(all_acts) >= total:
            break
        page += 1

    console.print(f"  Found {len(all_acts)} RTR activiteiten for {naam}")
    if not all_acts:
        return 0

    oin = all_acts[0].get("bestuursorgaan", {}).get("oin", "")

    count = 0
    with conn.cursor() as cur:
        for act in all_acts:
            omschrijving = act.get("omschrijving", "")
            for rbo in act.get("regelBeheerObjecten", []):
                fsr = rbo.get("functioneleStructuurRef", "")
                typering = rbo.get("typering", "")
                cur.execute(
                    """INSERT INTO i2a.regelbeheerobject
                       (functionele_structuur_ref, naam)
                       VALUES (%s, %s)
                       ON CONFLICT (functionele_structuur_ref) DO NOTHING""",
                    (fsr, f"{typering} - {omschrijving}"),
                )
                count += 1

    conn.commit()
    console.print(f"  [green]{count} regelBeheerObjecten geladen[/green]")
    return count, oin


def _load_sttr_regelbestanden(conn, oin: str, naam: str) -> int:
    """Load STTR toepasbare regelbestanden (metadata + DMN XML)."""
    if not oin:
        console.print(f"  [yellow]No OIN for {naam} — skipping STTR[/yellow]")
        return 0
    console.print(f"  Loading STTR regelbestanden for {naam} (OIN {oin[:12]}...)...")

    page = 1
    total_loaded = 0

    while True:
        data = _api_get(cfg.STTR_BASE, "/toepasbareRegels", {
            "datum": "10-04-2026",
            "oin": oin,
            "pageSize": 50,
            "page": page,
        })

        items = data.get("_embedded", {}).get("toepasbareRegelsList", [])
        if not items:
            # Try alternative key name
            items = data.get("_embedded", {}).get("toepasbareRegels", [])
        if not items:
            break

        total = data.get("page", {}).get("totalElements", 0)
        console.print(f"  Page {page}: {len(items)} items (total: {total})")

        with conn.cursor() as cur:
            for item in items:
                # Extract identifier from self link
                self_href = item.get("_links", {}).get("self", {}).get("href", "")
                # Pattern: .../toepasbareRegels/{id}?datum=...
                tr_id = None
                if "/toepasbareRegels/" in self_href:
                    tr_id = self_href.split("/toepasbareRegels/")[1].split("?")[0]

                fsr = item.get("functioneleStructuurRef", "")
                namespace = fsr  # Use functioneleStructuurRef as namespace/PK

                if not namespace:
                    continue

                # Ensure RBO exists (might be from another bestuursorgaan)
                if fsr:
                    cur.execute(
                        """INSERT INTO i2a.regelbeheerobject
                           (functionele_structuur_ref, naam)
                           VALUES (%s, %s)
                           ON CONFLICT (functionele_structuur_ref) DO NOTHING""",
                        (fsr, item.get("naam", item.get("omschrijving", ""))),
                    )

                # Insert regelbestand metadata
                cur.execute(
                    """INSERT INTO i2a.toepasbaar_regelbestand
                       (namespace, naam, regelbeheerobject)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (namespace) DO UPDATE SET naam = EXCLUDED.naam""",
                    (namespace,
                     item.get("naam", item.get("omschrijving", "")),
                     fsr if fsr else None),
                )

                # Download DMN XML if we have an ID
                if tr_id:
                    try:
                        with limiter:
                            xml_resp = httpx.get(
                                f"{cfg.STTR_BASE}/toepasbareRegels/{tr_id}/sttrBestand",
                                headers={"x-api-key": cfg.DSO_API_KEY},
                                timeout=30,
                            )

                        if xml_resp.status_code == 200:
                            _parse_and_store_dmn(conn, cur, namespace, xml_resp.content)
                    except Exception as e:
                        console.print(f"  [yellow]Warning: failed to download DMN for {tr_id}: {e}[/yellow]")

                total_loaded += 1

        conn.commit()

        # Check if there are more pages
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        page += 1

    console.print(f"  [green]{total_loaded} regelbestanden geladen[/green]")
    return total_loaded


def _parse_and_store_dmn(conn, cur, regelbestand_ns: str, xml_bytes: bytes):
    """Parse a DMN XML file and store decisions + uitvoeringsregels."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return

    # Extract decisions
    for decision in root.findall(".//semantic:decision", DMN_NS):
        dmn_id = decision.get("id", "")
        name = decision.get("name", "")

        cur.execute(
            """INSERT INTO i2a.dmn_element
               (regelbestand_ns, dmn_id, element_type, naam)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (regelbestand_ns, dmn_id) DO NOTHING""",
            (regelbestand_ns, dmn_id, "Decision", name),
        )

    # Extract inputData
    for inp in root.findall(".//semantic:inputData", DMN_NS):
        dmn_id = inp.get("id", "")
        name = inp.get("name", "")

        cur.execute(
            """INSERT INTO i2a.dmn_element
               (regelbestand_ns, dmn_id, element_type, naam)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (regelbestand_ns, dmn_id) DO NOTHING""",
            (regelbestand_ns, dmn_id, "InputData", name),
        )

    # Extract uitvoeringsregels
    for uitv in root.findall(".//uitv:uitvoeringsregel", DMN_NS):
        uitv_id = uitv.get("id", "")
        bereik = ""
        bereik_elem = uitv.find("uitv:bereik", DMN_NS)
        if bereik_elem is not None and bereik_elem.text:
            bereik = bereik_elem.text

        # Determine type from child elements
        regel_type = "Uitvoeringsregel"
        if uitv.find("uitv:vraag", DMN_NS) is not None:
            regel_type = "Vraag"
        elif uitv.find("uitv:rekenRegel", DMN_NS) is not None:
            regel_type = "RekenRegel"

        cur.execute(
            """INSERT INTO i2a.uitvoeringsregel
               (regelbestand_ns, regel_type)
               VALUES (%s, %s)""",
            (regelbestand_ns, regel_type),
        )


def _load_werkzaamheden(conn) -> dict:
    """Load all werkzaamheden and their activiteitKoppelingen."""
    console.print("  Loading werkzaamheden...")

    all_werkzaamheden = []
    page = 1
    while True:
        data = _api_get(cfg.RTR_BASE, "/werkzaamheden",
                        {"pageSize": 200, "page": page})
        items = data.get("_embedded", {}).get("werkzaamheden", [])
        all_werkzaamheden.extend(items)
        if not data.get("_links", {}).get("next"):
            break
        page += 1

    console.print(f"  Found {len(all_werkzaamheden)} werkzaamheden")

    stats = {"werkzaamheden": 0, "koppelingen": 0}

    with conn.cursor() as cur:
        for w in all_werkzaamheden:
            urn = w["urn"]
            naam = w.get("omschrijving", urn)
            cur.execute(
                """INSERT INTO i2a.werkzaamheid (urn, naam)
                   VALUES (%s, %s)
                   ON CONFLICT (urn) DO NOTHING""",
                (urn, naam),
            )
            stats["werkzaamheden"] += 1

    conn.commit()

    # Load activiteitKoppelingen per werkzaamheid
    console.print("  Loading activiteitKoppelingen...")
    linked = 0
    with conn.cursor() as cur:
        for w in all_werkzaamheden:
            urn = w["urn"]
            try:
                kdata = _api_get(cfg.RTR_BASE,
                                 f"/werkzaamheden/{urn}/activiteitKoppelingen",
                                 {"datum": "10-04-2026"})
                koppelingen = kdata.get("_embedded", {}).get("activiteitKoppelingen", [])
                for k in koppelingen:
                    act_urn = k.get("urn", "")
                    if act_urn:
                        cur.execute(
                            """UPDATE i2a.werkzaamheid SET activiteit_id = %s
                               WHERE urn = %s
                               AND EXISTS (SELECT 1 FROM p2p.activiteit WHERE identificatie = %s)""",
                            (act_urn, urn, act_urn),
                        )
                        if cur.rowcount > 0:
                            linked += 1
                            break
            except Exception:
                pass

    conn.commit()
    stats["koppelingen"] = linked
    console.print(f"  [green]{stats['werkzaamheden']} werkzaamheden, {linked} gekoppeld aan activiteiten[/green]")
    return stats


def load_imtr_for(organisatie_code: str, naam: str):
    """Load IMTR content for a specific bestuursorgaan."""
    conn = get_conn()
    try:
        result = _load_rtr_activiteiten(conn, organisatie_code, naam)
        if isinstance(result, tuple):
            count, oin = result
        else:
            count, oin = result, ""
        if oin:
            _load_sttr_regelbestanden(conn, oin, naam)
    finally:
        conn.close()


def load_imtr():
    """Load IMTR for the PoC municipality + werkzaamheden."""
    conn = get_conn()
    try:
        result = _load_rtr_activiteiten(conn, cfg.POC_CBS_CODE, cfg.POC_GEMEENTE_NAAM)
        if isinstance(result, tuple):
            count, oin = result
        else:
            count, oin = result, cfg.POC_OIN
        _load_sttr_regelbestanden(conn, oin or cfg.POC_OIN, cfg.POC_GEMEENTE_NAAM)
        _load_werkzaamheden(conn)
        console.print("[bold green]IMTR loading complete![/bold green]")
    finally:
        conn.close()
