"""Load IMTR content: RTR activiteiten + STTR regelbestanden.

Two APIs:
1. RTR v2: GET /activiteiten — list activities with metadata
2. STTR v1: GET /toepasbareRegels — list regelbestanden
              GET /toepasbareRegels/{id}/sttrBestand — download DMN XML

De delta (sinds 2026-08-08)
---------------------------
Waar de kosten zitten: niet in de lijsten maar in stap 2b. Per regelbestand
wordt een DMN-XML opgehaald en geparsed; bij ~150 bestanden per bronhouder en
343 bronhouders zijn dat ongeveer 50.000 downloads. Dat was de 5,6 uur van de
run van 2026-08-08. De lijst-calls zelf zijn ~1.000 stuks en verdwijnen in het
niet.

Waar de delta op rust: de STTR-lijst geeft per bestand een
`laatsteWijzigingDatum` op secondeniveau. Die slaan we op in
`i2a.toepasbaar_regelbestand.laatste_wijziging`; is hij bij een volgende run
gelijk, dan is de inhoud ongewijzigd en slaan we de XML over.

Drie keuzes die daarbij horen, elk met een reden:

* **Pas vastleggen ná een geslaagde verwerking.** Zou de datum al bij de
  metadata-insert worden gezet, dan zou een mislukte of afgebroken download de
  volgende run laten denken dat de inhoud er is — een stil gat van precies het
  type dat dit project vandaag vier keer heeft opgeleverd.
* **TEXT, geen timestamp.** De API levert `dd-mm-yyyy HH:MM:SS`. Exact
  vergelijken van de geleverde string is robuuster dan parsen met
  tijdzone-aannames; we hoeven er niet mee te rekenen, alleen op gelijkheid te
  toetsen.
* **Geen watermark over alle bestanden heen.** Eén datum per bestand, geen
  "alles sinds X". Dat is bewust: bij p2p bleek een watermark op
  registratietijdstip lek, omdat items later in de lijst kunnen verschijnen met
  een ouder tijdstip (zie `full_sync.fase_p2p`). Een vergelijking per bestand
  kent dat probleem niet.

Gemeten op gm1699 (148 regelbestanden), twee runs achter elkaar: 52,3 s → 3,1 s.

Wat de delta NIET dekt: verdwenen regelbestanden. Staat een bestand niet meer
in de lijst, dan blijft de rij in `i2a.toepasbaar_regelbestand` staan — zelfde
beperking als G-91 bij p2p, en een bewuste keuze: verdwijnen uit een lijst is
geen bewijs van intrekking.
"""

from lxml import etree

import re

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.http_retry import met_retry
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
    """GET request to a DSO API with shared rate limiting en retry op 5xx.

    De retry is hier op 2026-08-08 bijgekomen. `src/http_retry.py` bestond al
    sinds 01-08 en noemt in zijn eigen docstring dat er toen "één gemeente uit
    de i2a-fase viel" door een losse 503 — maar i2a gebruikte hem niet. Bij de
    run van 08-08 gebeurde precies hetzelfde: bronhouder 1699 sneuvelde op een
    503 bij `/activiteiten/_zoek`, 342 van 343 ok.

    De limiter zit binnen de retry-scope, dus een nieuwe poging gaat er netjes
    opnieuw doorheen.
    """
    url = f"{base_url}{path}"
    headers = {"x-api-key": cfg.DSO_API_KEY}

    def _doe():
        with limiter:
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    return met_retry(_doe, omschrijving=f"i2a GET {path}")


def _api_post(base_url: str, path: str, json_body: dict) -> dict:
    """POST request to a DSO API with shared rate limiting en retry op 5xx."""
    url = f"{base_url}{path}"
    headers = {"x-api-key": cfg.DSO_API_KEY, "Content-Type": "application/json"}

    def _doe():
        with limiter:
            resp = httpx.post(url, headers=headers, json=json_body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    return met_retry(_doe, omschrijving=f"i2a POST {path}")


def _rtr_organisatiecode(bronhouder_code: str) -> str:
    """De RTR wil de kale organisatiecode, zonder bestuurslaag-prefix.

    Gemeten 2026-08-08 tegen de live RTR: `gm0363` geeft 0 activiteiten,
    `0363` geeft er 113. Idem `gm0518`/`0518` (90), `ws0621`/`0621` (78),
    `pv27`/`27` (191). Het antwoord bevat de code zelf ook kaal
    (`{"oin": ..., "organisatieType": "MNRE", "organisatieCode": "1034"}`).

    Géén vaste breedte: `pv27` moet `27` worden, want `0027` geeft 0. Een code
    die al kaal is (zoals cfg.POC_CBS_CODE = "0344") blijft ongemoeid.

    Dit stond sinds de initial commit fout en heeft het per-bestuursorgaan-
    kanaal al die tijd leeg gehouden — zie vault gaps.md G-117.
    """
    return re.sub(r"^[a-z]+", "", bronhouder_code)


def _load_rtr_activiteiten(conn, organisatie_code: str, naam: str) -> tuple[int, str]:
    """Load RTR activiteiten for a bestuursorgaan via organisatieCode.

    Geeft altijd een tuple terug. Tot 2026-08-08 was dat bij een lege lijst
    een kale `0`, waardoor `load_imtr_for` `oin = ""` zette en de STTR-stap
    stilzwijgend oversloeg — zonder de "No OIN"-waarschuwing, want die staat
    in de functie die dan niet meer wordt aangeroepen.
    """
    console.print(f"  Loading RTR activiteiten for {naam}...")
    rtr_code = _rtr_organisatiecode(organisatie_code)

    all_acts = []
    page = 1
    while True:
        data = _api_post(cfg.RTR_BASE, "/activiteiten/_zoek", {
            "datum": "10-04-2026",
            "bestuursorgaan": {"organisatieCode": rtr_code},
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
        console.print(f"  [yellow]0 RTR activiteiten voor {naam} "
                      f"(organisatieCode {rtr_code}) — STTR wordt overgeslagen[/yellow]")
        return 0, ""

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
    n_overgeslagen = 0

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

                # ── delta: sla de DMN-download over als er niets is gewijzigd ──
                # Het dure deel van deze fase is niet de lijst maar de XML: één
                # call per regelbestand, ~50.000 per sync, samen 5,6 uur. De
                # lijst levert `laatsteWijzigingDatum` op secondeniveau; is die
                # gelijk aan wat we bij de vorige geslaagde verwerking hebben
                # opgeslagen, dan is de inhoud ongewijzigd.
                gewijzigd_op = item.get("laatsteWijzigingDatum")
                cur.execute("SELECT laatste_wijziging FROM i2a.toepasbaar_regelbestand "
                            "WHERE namespace = %s", (namespace,))
                rij = cur.fetchone()
                bekend = rij["laatste_wijziging"] if rij else None
                if gewijzigd_op and bekend and bekend == gewijzigd_op:
                    n_overgeslagen += 1
                    total_loaded += 1
                    continue

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
                            # Pas ná geslaagde verwerking vastleggen — anders zou
                            # een mislukte download de volgende run laten denken
                            # dat de inhoud er is.
                            if gewijzigd_op:
                                cur.execute(
                                    "UPDATE i2a.toepasbaar_regelbestand "
                                    "   SET laatste_wijziging = %s WHERE namespace = %s",
                                    (gewijzigd_op, namespace))
                    except Exception as e:
                        console.print(f"  [yellow]Warning: failed to download DMN for {tr_id}: {e}[/yellow]")

                total_loaded += 1

        conn.commit()

        # Check if there are more pages
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        page += 1

    if n_overgeslagen:
        console.print(f"  [green]{total_loaded} regelbestanden "
                      f"({n_overgeslagen} ongewijzigd, DMN overgeslagen)[/green]")
    else:
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
