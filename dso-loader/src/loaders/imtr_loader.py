"""Load IMTR content: RTR activiteiten + STTR regelbestanden.

Two APIs:
1. RTR v2: GET /activiteiten — list activities with metadata
2. STTR v1: GET /toepasbareRegels — list regelbestanden
              GET /toepasbareRegels/{id}/sttrBestand — download DMN XML
"""

import re

from lxml import etree

import httpx
from psycopg.types.json import Json
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


def _eltext(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _feel_cell(s: str):
    """Parse één FEEL-cel uit een beslistabel (subset; spiegelt trcg.dmn.reduce)."""
    s = (s or "").strip()
    if s in ("", "-"):
        return None
    if s in ("true", "false"):
        return s == "true"
    if s.startswith("[") and s.endswith("]"):
        return {"IN": [v.strip().strip('"').strip("'") for v in s[1:-1].split(",")]}
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if re.match(r"^[<>]=?\s*\d", s):
        m = re.match(r"^([<>]=?)\s*(\d+(?:\.\d+)?)$", s)
        if m:
            return {m.group(1): float(m.group(2))}
    return s


# DMN-MODEL-namespaces: 2015 (semantic:) én 2018 (dmn:) komen beide voor in de
# echte aanleveringen. We zoeken elementen onder beide URI's (Clark-notatie),
# net als trcg.dmn.reduce.
_DMN_MODEL_NS = (
    "http://www.omg.org/spec/DMN/20151101/dmn.xsd",
    "http://www.omg.org/spec/DMN/20180521/MODEL/",
)


def _dmn_findall(el, tag: str):
    for uri in _DMN_MODEL_NS:
        r = el.findall(f".//{{{uri}}}{tag}")
        if r:
            return r
    return []


def _dmn_children(el, tag: str):
    for uri in _DMN_MODEL_NS:
        r = el.findall(f"{{{uri}}}{tag}")
        if r:
            return r
    return []


def _dmn_child(el, tag: str):
    for uri in _DMN_MODEL_NS:
        r = el.find(f"{{{uri}}}{tag}")
        if r is not None:
            return r
    return None


def _build_beslisgraaf(root, dmn_type: str = "Outcome") -> dict:
    """Reduceer DMN-XML naar de niveau-B beslisgraaf (zelfde vorm als trcg.dmn.DecisionGraph).

    Deterministisch, lxml, dual-namespace (DMN 2015 + 2018). Bewust een kopie van
    de logica in `trcg/dmn/reduce.py` (geen cross-repo dependency); de *vorm* is
    het contract, niet de code.
    """
    decisions = _dmn_findall(root, "decision")
    inputdatas = _dmn_findall(root, "inputData")

    dec_id2name = {d.get("id"): (d.get("name") or d.get("id")) for d in decisions}
    inp_id2name = {}
    for i in inputdatas:
        var = _dmn_child(i, "variable")
        inp_id2name[i.get("id")] = (
            var.get("name") if var is not None else (i.get("name") or i.get("id"))
        )

    ext: dict[str, dict] = {}
    for i in inputdatas:
        var = _dmn_child(i, "variable")
        name = (var.get("name") if var is not None else None) or i.get("name")
        if not name:
            continue
        tref = (var.get("typeRef") if var is not None else "string") or "string"
        ext[name] = {"id": name, "type": "boolean" if "boolean" in tref else "string", "domain": []}

    nodes = []
    for dec in decisions:
        name = dec.get("name") or dec.get("id")
        table = _dmn_child(dec, "decisionTable")
        if table is None:
            continue
        hit = table.get("hitPolicy", "FIRST")
        cols = []
        for ic in _dmn_children(table, "input"):
            ie = _dmn_child(ic, "inputExpression")
            txt = _eltext(_dmn_child(ie, "text")) if ie is not None else ""
            if txt:
                cols.append(txt)
        logic = []
        for rule in _dmn_children(table, "rule"):
            ins = [_feel_cell(_eltext(x)) for x in _dmn_children(rule, "inputEntry")]
            outs = [_eltext(x) for x in _dmn_children(rule, "outputEntry")]
            when = {}
            for col, val in zip(cols, ins):
                if val is None:
                    continue
                when[col] = val
            out_val = None
            if outs:
                parsed = [_feel_cell(o) for o in outs if o.strip()]
                out_val = parsed[0] if len(parsed) == 1 else parsed
            logic.append({"when": when, "then": {"value": out_val}})
        nodes.append({"id": name, "inputs": cols, "hit_policy": hit, "logic": logic})

    edges = []
    for dec in decisions:
        dst = dec.get("name") or dec.get("id")
        for inf in _dmn_children(dec, "informationRequirement"):
            for tag, mapper in (("requiredDecision", dec_id2name), ("requiredInput", inp_id2name)):
                el = _dmn_child(inf, tag)
                if el is not None:
                    ref = (el.get("href") or "").lstrip("#")
                    edges.append([mapper.get(ref, ref), dst])

    dec_names = {n["id"] for n in nodes}
    out_srcs = {src for src, _ in edges if src in dec_names}
    sinks = [n for n in dec_names if n not in out_srcs]
    cands = [n for n in sinks if dmn_type.replace("_", " ") in n]
    top = cands[0] if cands else (sinks[0] if sinks else (next(iter(dec_names), None)))

    return {
        "type": dmn_type,
        "top_node": top,
        "external_variables": list(ext.values()),
        "nodes": nodes,
        "edges": edges,
    }


def _parse_and_store_dmn(conn, cur, regelbestand_ns: str, xml_bytes: bytes):
    """Parse a DMN XML file and store decisions + uitvoeringsregels + beslisgraaf."""
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

    # Reduceer de volledige DMN naar de uitvoerbare beslisgraaf en bewaar 'm.
    try:
        graaf = _build_beslisgraaf(root)
        n_dec = len(graaf["nodes"])
        n_regels = sum(len(n["logic"]) for n in graaf["nodes"])
        cur.execute(
            """UPDATE i2a.toepasbaar_regelbestand
               SET beslisgraaf = %s, aantal_decisions = %s,
                   aantal_regels = %s, heeft_logica = %s
               WHERE namespace = %s""",
            (Json(graaf), n_dec, n_regels, n_regels > 0, regelbestand_ns),
        )
    except Exception as e:  # noqa: BLE001 — graaf-reductie mag de load niet breken
        console.print(f"  [yellow]Beslisgraaf-reductie faalde voor {regelbestand_ns}: {e}[/yellow]")


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
