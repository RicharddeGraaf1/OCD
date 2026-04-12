"""Load Ow regelingen via Presenteren v8 + Geometrie Opvragen v1 APIs.

Pure API pipeline — no ZIP downloads, no XML parsing.

Flow per regeling:
1. GET /regelingen?bevoegdGezag={code}  → regelingen-lijst
2. GET /regelingen/{id}?_expand=true  → pons + regelingsgebied
3. GET /regelingen/{id}/documentstructuur  → tekst_element
4. GET /regelingen/{id}/regeltekstannotaties?locatieSelectie=primair  → OW-objecten
   of  /regelingen/{id}/divisieannotaties?locatieSelectie=primair
   (activiteiten, gebiedsaanwijzingen, omgevingsnormen, omgevingswaarden, kaarten)
5. GET /geometrieen/{uuid}?crs=...  → GeoJSON per locatie
"""

import json
import re

import httpx
from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.rate_limiter import limiter

console = Console()

CRS_RD = "http://www.opengis.net/def/crs/EPSG/0/28992"


def _headers():
    return {"X-Api-Key": cfg.DSO_API_KEY}


def _get(url, params=None, timeout=60):
    with limiter:
        resp = httpx.get(url, headers=_headers(), params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_geometry(geom_id: str) -> dict | None:
    """Fetch GeoJSON geometry by geometrieIdentificatie UUID."""
    try:
        url = f"{cfg.GEOMETRIE_BASE}/geometrieen/{geom_id}"
        with limiter:
            resp = httpx.get(url, headers=_headers(), params={"crs": CRS_RD}, timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        console.print(f"      [dim]Geometry fetch failed for {geom_id}: {e}[/dim]")
    return None


# ── Regelingen discovery ────────────────────────────────────────────

def find_regelingen(overheid_code: str, naam: str,
                    doc_types: list[str] | None = None) -> list[dict]:
    """Find all regelingen for a given overheid via Presenteren API."""
    console.print(f"  Searching regelingen for {naam} ({overheid_code})...")
    results = []

    for page in range(1, 500):
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen",
                    params={"bevoegdGezag": overheid_code, "page": page, "size": 200})
        for reg in data.get("_embedded", {}).get("regelingen", []):
            if reg.get("aangeleverdDoorEen", {}).get("code") != overheid_code:
                continue
            reg_type = reg.get("type", {}).get("waarde", "")
            if doc_types and reg_type not in doc_types:
                continue
            ident = reg["identificatie"]
            if not any(r["identificatie"] == ident for r in results):
                results.append({
                    "identificatie": ident,
                    "titel": reg.get("officieleTitel", ""),
                    "type": reg_type,
                    "expressionId": reg.get("expressionId", ""),
                    "aangeleverdDoorEen": reg.get("aangeleverdDoorEen", {}),
                })
                console.print(f"    Found: [{reg_type}] {reg.get('officieleTitel', '')[:60]}")
        if not data.get("_links", {}).get("next", {}).get("href"):
            break

    console.print(f"  [green]Found {len(results)} regelingen[/green]")
    return results


# ── Documentstructuur ────────────────────────────────────────────────

def _parse_kop(kop_xml: str | None) -> tuple[str | None, str | None]:
    """Extract Nummer and Opschrift from STOP <Kop> XML snippet."""
    if not kop_xml:
        return None, None
    nummer = None
    opschrift = None
    m = re.search(r"<Nummer>(.*?)</Nummer>", kop_xml)
    if m:
        nummer = m.group(1).strip()
    m = re.search(r"<Opschrift>(.*?)</Opschrift>", kop_xml)
    if m:
        opschrift = m.group(1).strip()
    return nummer, opschrift


API_TYPE_TO_STOP = {
    "LICHAAM": "Lichaam",
    "BOEK": "Boek",
    "DEEL": "Deel",
    "HOOFDSTUK": "Hoofdstuk",
    "AFDELING": "Afdeling",
    "TITEL": "Titel",
    "PARAGRAAF": "Paragraaf",
    "SUBPARAGRAAF": "Subparagraaf",
    "SUBSUBPARAGRAAF": "Subsubparagraaf",
    "ARTIKEL": "Artikel",
    "LID": "Lid",
    "DIVISIE": "Divisie",
    "DIVISIETEKST": "Divisietekst",
    "BIJLAGE": "Bijlage",
    "TOELICHTING": "Toelichting",
    "ALGEMENETOELICHTING": "AlgemeneToelichting",
    "ARTIKELGEWIJZETOELICHTING": "ArtikelgewijzeToelichting",
    "REGELINGOPSCHRIFT": "RegelingOpschrift",
    "BEGRIP": "Begrip",
}


def _flatten_components(components: list[dict], parent_eid: str | None = None,
                        volgorde_offset: int = 0) -> list[dict]:
    """Recursively flatten nested DocumentComponent tree into a flat list."""
    result = []
    for i, comp in enumerate(components):
        eid = comp.get("expressie", "")
        wid = comp.get("identificatie", eid)
        raw_type = comp.get("type", "ONBEKEND")
        comp_type = API_TYPE_TO_STOP.get(raw_type, raw_type)
        nummer, opschrift = _parse_kop(comp.get("kop"))
        inhoud = comp.get("inhoud")

        result.append({
            "eid": eid,
            "wid": wid,
            "element_type": comp_type,
            "parent_eid": parent_eid,
            "nummer": nummer,
            "opschrift": opschrift,
            "inhoud": inhoud,
            "volgorde": volgorde_offset + i,
        })

        children = comp.get("_embedded", {}).get("documentComponenten", [])
        if children:
            result.extend(_flatten_components(children, parent_eid=eid))

    return result


def load_documentstructuur(conn, regeling_uri: str, expression_id: str):
    """Load document structure via Presenteren API."""
    encoded = regeling_uri.replace("/", "_")
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/documentstructuur")

    top_components = data.get("_embedded", {}).get("documentComponenten", [])
    elements = _flatten_components(top_components)

    if not elements:
        return 0

    with conn.cursor() as cur:
        eid_to_id = {}
        for elem in elements:
            cur.execute(
                """INSERT INTO dso.tekst_element
                   (regeling_expression, eid, wid, element_type, nummer, opschrift, inhoud, volgorde)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (regeling_expression, eid) DO NOTHING
                   RETURNING id""",
                (expression_id, elem["eid"], elem["wid"], elem["element_type"],
                 elem["nummer"], elem["opschrift"], elem["inhoud"], elem["volgorde"]),
            )
            row = cur.fetchone()
            if row:
                eid_to_id[elem["eid"]] = row["id"]

        for elem in elements:
            if elem["parent_eid"] and elem["eid"] in eid_to_id and elem["parent_eid"] in eid_to_id:
                cur.execute(
                    "UPDATE dso.tekst_element SET parent_id = %s WHERE id = %s",
                    (eid_to_id[elem["parent_eid"]], eid_to_id[elem["eid"]]),
                )

    conn.commit()
    return len(elements)


# ── Annotaties (artikelstructuur) ────────────────────────────────────

def load_regeltekstannotaties(conn, regeling_uri: str, bronhouder: str):
    """Load regeltekstannotaties (artikelstructuur) via Presenteren API."""
    encoded = regeling_uri.replace("/", "_")
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/regeltekstannotaties",
                params={"locatieSelectie": "primair"})

    stats = {"activiteiten": 0, "regels": 0, "ala": 0, "ga": 0,
             "locaties": 0, "geometrieen": 0, "normen": 0, "normwaarden": 0,
             "kaarten": 0, "kaartlagen": 0}

    # ── Build regeltekst ID → STOP wId mapping ──
    rt_to_wid = {}
    for rt in data.get("regelteksten", []):
        rt_to_wid[rt["identificatie"]] = rt.get("wId", "")

    with conn.cursor() as cur:
        # ── Locaties (primair: mostly Gebiedengroepen + Ambtsgebied) ──
        locaties = data.get("locaties", [])
        for loc in locaties:
            loc_id = loc["identificatie"]
            loc_type = loc["locatieType"]
            noemer = loc.get("noemer")
            geom_id = loc.get("geometrieIdentificatie")

            geojson = _get_geometry(geom_id) if geom_id else None
            if geojson:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
                    (loc_id, loc_type, noemer, json.dumps(geojson), json.dumps(geojson)),
                )
                stats["geometrieen"] += 1
            else:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id, loc_type, noemer),
                )
            stats["locaties"] += 1

        # ── Activiteiten ──
        for act in data.get("activiteiten", []):
            cur.execute(
                """INSERT INTO dso.activiteit (identificatie, naam, groep, is_tophaak)
                   VALUES (%s, %s, %s, false)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (act["identificatie"], act["naam"],
                 act.get("groep", {}).get("waarde") if isinstance(act.get("groep"), dict) else act.get("groep")),
            )
            stats["activiteiten"] += 1

        for act in data.get("activiteiten", []):
            parent = act.get("bovenliggendeActiviteitRef")
            if parent:
                cur.execute(
                    """UPDATE dso.activiteit SET bovenliggende = %s
                       WHERE identificatie = %s
                       AND EXISTS (SELECT 1 FROM dso.activiteit WHERE identificatie = %s)""",
                    (parent, act["identificatie"], parent),
                )

        # ── Gebiedsaanwijzingen ──
        for ga in data.get("gebiedsaanwijzingen", []):
            ga_type = ga.get("type", {}).get("waarde", "") if isinstance(ga.get("type"), dict) else ga.get("type", "")
            ga_groep = ga.get("groep", {}).get("waarde", "") if isinstance(ga.get("groep"), dict) else ga.get("groep", "")
            ga_locs = ga.get("locatieRefs", [])
            loc_id = ga_locs[0] if ga_locs else None
            if loc_id:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            cur.execute(
                """INSERT INTO dso.gebiedsaanwijzing (identificatie, type, naam, groep, locatie_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (ga["identificatie"], ga_type, ga.get("naam", ""), ga_groep, loc_id),
            )
            stats["ga"] += 1

        # ── Juridische regels + ALA's ──
        for regel_type_key in ("regelsVoorIedereen", "instructieregels", "omgevingswaarderegels"):
            regels = data.get(regel_type_key, [])
            regel_type_map = {
                "regelsVoorIedereen": "RegelVoorIedereen",
                "instructieregels": "Instructieregel",
                "omgevingswaarderegels": "Omgevingswaardegel",
            }
            regel_type = regel_type_map.get(regel_type_key, "RegelVoorIedereen")

            for regel in regels:
                idealisatie = regel.get("idealisatie", {}).get("waarde") if isinstance(regel.get("idealisatie"), dict) else regel.get("idealisatie")
                regeltekst_ref = regel.get("regeltekstRef", "")
                regeltekst_wid = rt_to_wid.get(regeltekst_ref, regeltekst_ref)

                cur.execute(
                    """INSERT INTO dso.juridische_regel
                       (identificatie, regel_type, idealisatie, regeltekst_wid)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (regel["identificatie"], regel_type, idealisatie, regeltekst_wid),
                )
                stats["regels"] += 1

                # GA-junction
                for ga_ref in regel.get("gebiedsaanwijzingRefs", []):
                    cur.execute(
                        """INSERT INTO dso.juridische_regel_gebiedsaanwijzing
                           (juridische_regel_id, gebiedsaanwijzing_id)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (regel["identificatie"], ga_ref),
                    )

                # ALA's
                for ala in regel.get("activiteitLocatieaanduidingen", []):
                    act_ref = ala.get("activiteitRef", "")
                    kwal = ala.get("activiteitregelkwalificatie", {})
                    kwal_val = kwal.get("waarde") if isinstance(kwal, dict) else kwal
                    for loc_ref in ala.get("locatieRefs", []):
                        cur.execute(
                            """INSERT INTO dso.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT (identificatie) DO NOTHING""",
                            (loc_ref,),
                        )
                        cur.execute(
                            """INSERT INTO dso.activiteit_locatieaanduiding
                               (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                               VALUES (%s, %s, %s, %s)""",
                            (regel["identificatie"], act_ref, loc_ref, kwal_val),
                        )
                        stats["ala"] += 1

        # ── Omgevingsnormen + Omgevingswaarden ──
        for norm_type_key, norm_type_label in (("omgevingsnormen", "Omgevingsnorm"),
                                                ("omgevingswaarden", "Omgevingswaarde")):
            for norm in data.get(norm_type_key, []):
                n_type = norm.get("type", {})
                n_type_val = n_type.get("waarde") if isinstance(n_type, dict) else n_type
                n_eenheid = norm.get("eenheid", {})
                n_eenheid_val = n_eenheid.get("waarde") if isinstance(n_eenheid, dict) else n_eenheid
                n_groep = norm.get("groep", {})
                n_groep_val = n_groep.get("waarde") if isinstance(n_groep, dict) else n_groep

                cur.execute(
                    """INSERT INTO dso.norm
                       (identificatie, norm_type, naam, type_norm, eenheid, groep)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (norm["identificatie"], norm_type_label, norm["naam"],
                     n_type_val, n_eenheid_val, n_groep_val),
                )
                stats["normen"] += 1

                for nw in norm.get("normwaarden", []):
                    nw_locs = nw.get("locatieRefs", [])
                    for nw_loc in nw_locs:
                        cur.execute(
                            """INSERT INTO dso.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT (identificatie) DO NOTHING""",
                            (nw_loc,),
                        )
                        cur.execute(
                            """INSERT INTO dso.normwaarde
                               (norm_id, locatie_id, kwalitatieve_waarde, kwantitatieve_waarde)
                               VALUES (%s, %s, %s, %s)""",
                            (norm["identificatie"], nw_loc,
                             nw.get("kwalitatieveWaarde"),
                             nw.get("kwantitatieveWaarde")),
                        )
                        stats["normwaarden"] += 1

        # ── Juridische regel → norm junctions ──
        for regel_type_key in ("regelsVoorIedereen", "instructieregels", "omgevingswaarderegels"):
            norm_ref_key = "omgevingswaardeRefs" if regel_type_key == "omgevingswaarderegels" else "omgevingsnormRefs"
            for regel in data.get(regel_type_key, []):
                for norm_ref in regel.get(norm_ref_key, []):
                    cur.execute(
                        """INSERT INTO dso.juridische_regel_norm
                           (juridische_regel_id, norm_id)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (regel["identificatie"], norm_ref),
                    )

        # ── Kaarten ──
        for kaart in data.get("kaarten", []):
            cur.execute(
                """INSERT INTO dso.kaart (identificatie, naam)
                   VALUES (%s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (kaart["identificatie"], kaart.get("naam", "")),
            )
            stats["kaarten"] += 1

            for kl in kaart.get("kaartlagen", []):
                ga_refs = kl.get("gebiedsaanwijzingRefs", [])
                norm_refs = kl.get("omgevingsnormRefs", [])
                ala_refs = kl.get("activiteitLocatieaanduidingRefs", [])
                cur.execute(
                    """INSERT INTO dso.kaartlaag (kaart_id, naam, gebiedsaanwijzing_id, norm_id, activiteit_id)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (kaart["identificatie"],
                     kl.get("naam", ""),
                     ga_refs[0] if ga_refs else None,
                     norm_refs[0] if norm_refs else None,
                     ala_refs[0] if ala_refs else None),
                )
                stats["kaartlagen"] += 1

    conn.commit()
    return stats


# ── Annotaties (vrijetekststructuur) ─────────────────────────────────

def load_divisieannotaties(conn, regeling_uri: str, bronhouder: str):
    """Load divisieannotaties (vrijetekststructuur) via Presenteren API."""
    encoded = regeling_uri.replace("/", "_")
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/divisieannotaties",
                params={"locatieSelectie": "primair"})

    stats = {"tekstdelen": 0, "locaties": 0, "geometrieen": 0, "ga": 0,
             "hoofdlijnen": 0, "kaarten": 0, "kaartlagen": 0}

    with conn.cursor() as cur:
        # ── Locaties ──
        for loc in data.get("locaties", []):
            loc_id = loc["identificatie"]
            geom_id = loc.get("geometrieIdentificatie")

            geojson = _get_geometry(geom_id) if geom_id else None
            if geojson:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
                    (loc_id, loc["locatieType"], loc.get("noemer"),
                     json.dumps(geojson), json.dumps(geojson)),
                )
                stats["geometrieen"] += 1
            else:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id, loc["locatieType"], loc.get("noemer")),
                )
            stats["locaties"] += 1

        # ── Gebiedsaanwijzingen ──
        for ga in data.get("gebiedsaanwijzingen", []):
            ga_type = ga.get("type", {}).get("waarde", "") if isinstance(ga.get("type"), dict) else ga.get("type", "")
            ga_groep = ga.get("groep", {}).get("waarde", "") if isinstance(ga.get("groep"), dict) else ga.get("groep", "")
            ga_locs = ga.get("locatieRefs", [])
            loc_id = ga_locs[0] if ga_locs else None
            if loc_id:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            cur.execute(
                """INSERT INTO dso.gebiedsaanwijzing (identificatie, type, naam, groep, locatie_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (ga["identificatie"], ga_type, ga.get("naam", ""), ga_groep, loc_id),
            )
            stats["ga"] += 1

        # ── Tekstdelen ──
        for td in data.get("tekstdelen", []):
            loc_refs = td.get("locatieRefs", [])
            loc_id = loc_refs[0] if loc_refs else None
            if loc_id:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            themas = None
            if td.get("themas"):
                themas = [t.get("waarde", t) if isinstance(t, dict) else t for t in td["themas"]]

            cur.execute(
                """INSERT INTO dso.tekstdeel (identificatie, divisie_wid, thema, locatie_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (td["identificatie"], td.get("divisietekstRef", ""), themas, loc_id),
            )
            stats["tekstdelen"] += 1

        # ── Hoofdlijnen ──
        for hl in data.get("hoofdlijnen", []):
            cur.execute(
                """INSERT INTO dso.hoofdlijn (identificatie, soort, naam)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (hl["identificatie"], hl.get("soort", ""), hl.get("naam", "")),
            )
            stats["hoofdlijnen"] += 1

        # ── Kaarten ──
        for kaart in data.get("kaarten", []):
            cur.execute(
                """INSERT INTO dso.kaart (identificatie, naam)
                   VALUES (%s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (kaart["identificatie"], kaart.get("naam", "")),
            )
            stats["kaarten"] += 1

            for kl in kaart.get("kaartlagen", []):
                ga_refs = kl.get("gebiedsaanwijzingRefs", [])
                cur.execute(
                    """INSERT INTO dso.kaartlaag (kaart_id, naam, gebiedsaanwijzing_id)
                       VALUES (%s, %s, %s)""",
                    (kaart["identificatie"],
                     kl.get("naam", ""),
                     ga_refs[0] if ga_refs else None),
                )
                stats["kaartlagen"] += 1

    conn.commit()
    return stats


# ── Pons + Regelingsgebied (via _expand) ────────────────────────────

def load_regeling_expand(conn, regeling_uri: str, expression_id: str):
    """Load pons and regelingsgebied via GET /regelingen/{id}?_expand=true."""
    encoded = regeling_uri.replace("/", "_")
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}",
                params={"_expand": "true"})

    stats = {"regelingsgebied": False, "pons": False}
    embedded = data.get("_embedded", {})

    with conn.cursor() as cur:
        # ── TijdelijkDeelVan ──
        tdv_link = data.get("_links", {}).get("tijdelijkDeelVan", {}).get("href", "")
        if tdv_link:
            # Extract the work-id from the href: .../regelingen/_akn_nl_act_...?...
            tdv_part = tdv_link.split("/regelingen/")[-1].split("?")[0]
            tdv_work = tdv_part.replace("_", "/")
            cur.execute(
                "UPDATE dso.regeling SET is_tijdelijkdeel_van = %s WHERE frbr_expression = %s",
                (tdv_work, expression_id),
            )

        # ── Regelingsgebied ──
        rg = embedded.get("regelingsgebied")
        if rg:
            rg_id = rg["identificatie"]
            geom_id = rg.get("geometrieIdentificatie")
            geojson = _get_geometry(geom_id) if geom_id else None
            if geojson:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
                    (rg_id, rg.get("locatieType", "Regelingsgebied"), rg.get("noemer"),
                     json.dumps(geojson), json.dumps(geojson)),
                )
            else:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (rg_id, rg.get("locatieType", "Regelingsgebied"), rg.get("noemer")),
                )
            cur.execute(
                "UPDATE dso.regeling SET regelingsgebied_id = %s WHERE frbr_expression = %s",
                (rg_id, expression_id),
            )
            stats["regelingsgebied"] = True

        # ── Pons ──
        pons = embedded.get("pons")
        if pons:
            pons_id = pons["identificatie"]
            geom_id = pons.get("geometrieIdentificatie")
            geojson = _get_geometry(geom_id) if geom_id else None
            if geojson:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
                    (pons_id, pons.get("locatieType", "Pons"), pons.get("noemer"),
                     json.dumps(geojson), json.dumps(geojson)),
                )
            else:
                cur.execute(
                    """INSERT INTO dso.locatie (identificatie, locatie_type, noemer, geometrie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (pons_id, pons.get("locatieType", "Pons"), pons.get("noemer")),
                )
            cur.execute(
                """INSERT INTO dso.pons (identificatie, locatie_id)
                   VALUES (%s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (pons_id, pons_id),
            )
            stats["pons"] = True

    conn.commit()
    return stats


# ── Regelingmodel detection ──────────────────────────────────────────

ARTIKELSTRUCTUUR_TYPES = {
    "Omgevingsplan", "Omgevingsverordening", "Waterschapsverordening",
    "AMvB", "Ministeriele regeling", "Projectbesluit",
    "Voorbereidingsbesluit", "Voorbeschermingsregels",
    "Voorbeschermingsregels Omgevingsplan",
    "Voorbeschermingsregels Omgevingsverordening",
    "Reactieve interventie",
}

VRIJETEKST_TYPES = {
    "Omgevingsvisie", "Programma", "Instructie", "Natura 2000-besluit",
}

REGELINGMODEL_MAP = {
    "Omgevingsplan": "RegelingCompact",
    "Omgevingsverordening": "RegelingCompact",
    "Waterschapsverordening": "RegelingCompact",
    "AMvB": "RegelingCompact",
    "Omgevingsvisie": "RegelingVrijetekst",
    "Programma": "RegelingVrijetekst",
    "Instructie": "RegelingVrijetekst",
    "Voorbereidingsbesluit": "RegelingTijdelijkdeel",
    "Voorbeschermingsregels": "RegelingTijdelijkdeel",
    "Voorbeschermingsregels Omgevingsplan": "RegelingTijdelijkdeel",
    "Voorbeschermingsregels Omgevingsverordening": "RegelingTijdelijkdeel",
    "Projectbesluit": "RegelingCompact",
    "Reactieve interventie": "RegelingCompact",
    "Natura 2000-besluit": "RegelingVrijetekst",
}


# ── Main entry point ─────────────────────────────────────────────────

def load_via_api(overheid_code: str, naam: str,
                 bronhouder_code: str | None = None,
                 doc_types: list[str] | None = None):
    """Load all regelingen for an overheid via API pipeline."""
    if bronhouder_code is None:
        bronhouder_code = overheid_code

    conn = get_conn()
    try:
        regelingen = find_regelingen(overheid_code, naam, doc_types)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dso.bronhouder (overheidscode, naam) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (bronhouder_code, naam),
            )
        conn.commit()

        for reg in regelingen:
            regeling_uri = reg["identificatie"]
            expression_id = reg.get("expressionId", regeling_uri)
            doc_type = reg["type"]
            regelingmodel = REGELINGMODEL_MAP.get(doc_type, "RegelingCompact")

            console.print(f"\n  [bold]Loading: {reg['titel'][:60]}[/bold] ({doc_type})")

            # ── Regeling metadata ──
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO dso.regeling
                       (frbr_expression, frbr_work, regelingmodel, opschrift, citeertitel, bronhouder, documenttype)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (frbr_expression) DO NOTHING""",
                    (expression_id, regeling_uri, regelingmodel,
                     reg.get("titel", ""), reg.get("titel", ""),
                     bronhouder_code, doc_type),
                )
            conn.commit()

            # ── Documentstructuur ──
            try:
                n_tekst = load_documentstructuur(conn, regeling_uri, expression_id)
                console.print(f"    Documentstructuur: {n_tekst} elementen")
            except Exception as e:
                console.print(f"    [red]Documentstructuur failed: {e}[/red]")

            # ── Pons + Regelingsgebied ──
            try:
                expand_stats = load_regeling_expand(conn, regeling_uri, expression_id)
                parts = []
                if expand_stats["regelingsgebied"]:
                    parts.append("regelingsgebied")
                if expand_stats["pons"]:
                    parts.append("pons")
                if parts:
                    console.print(f"    Expand: {', '.join(parts)}")
            except Exception as e:
                console.print(f"    [dim]Expand failed: {e}[/dim]")

            # ── Annotaties ──
            try:
                if doc_type in ARTIKELSTRUCTUUR_TYPES:
                    stats = load_regeltekstannotaties(conn, regeling_uri, bronhouder_code)
                    console.print(
                        f"    Annotaties: {stats['regels']} regels, "
                        f"{stats['activiteiten']} activiteiten, {stats['ala']} ALA's, "
                        f"{stats['ga']} GA's, {stats['normen']} normen "
                        f"({stats['normwaarden']} waarden), "
                        f"{stats['kaarten']} kaarten, "
                        f"{stats['locaties']} locaties "
                        f"({stats['geometrieen']} met geometrie)")
                elif doc_type in VRIJETEKST_TYPES:
                    stats = load_divisieannotaties(conn, regeling_uri, bronhouder_code)
                    console.print(
                        f"    Annotaties: {stats['tekstdelen']} tekstdelen, "
                        f"{stats['ga']} GA's, {stats['kaarten']} kaarten, "
                        f"{stats['locaties']} locaties "
                        f"({stats['geometrieen']} met geometrie)")
                else:
                    console.print(f"    [yellow]Unknown type {doc_type}, trying artikelstructuur[/yellow]")
                    stats = load_regeltekstannotaties(conn, regeling_uri, bronhouder_code)
            except Exception as e:
                console.print(f"    [red]Annotaties failed: {e}[/red]")

        console.print(f"\n[green]Done: {len(regelingen)} regelingen loaded for {naam}[/green]")

    finally:
        conn.close()
