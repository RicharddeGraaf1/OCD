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

from src.canonieke_bronhouders import upsert_bronhouder
from src.config import cfg

# Regelingen waarvan de annotatielaag is omgevallen. `full_sync.fase_p2p` leest
# deze lijst en zet hem in het sync-rapport; zonder dat blijft zo'n mislukking
# een gekleurde regel in de uitvoer die niemand terugziet.
ANNOTATIE_FOUTEN: list[tuple[str, str]] = []
from src.db import get_conn
from src.http_retry import met_retry
from src.rate_limiter import limiter
from src.versie_status import markeer_siblings_inactief

console = Console()

CRS_RD = "http://www.opengis.net/def/crs/EPSG/0/28992"


def _encode_regeling_uri(regeling_uri: str) -> str:
    """Encode een frbr-work tot DSO-pad-segment.

    DSO Presenteren v8 vervangt zowel `/` als `-` door `_` in regelingen-paden;
    alleen `/`-substitutie geeft 404 voor regelingen met date- of UUID-segmenten
    (bv. `/akn/nl/act/ws0653/2023-12-05/fdff9a72-7954-...`).
    """
    return regeling_uri.replace("/", "_").replace("-", "_")


def _headers():
    return {"X-Api-Key": cfg.DSO_API_KEY}


def _get(url, params=None, timeout=60):
    """GET op de Presenteren-API, met beperkte retry op transiënte fouten.

    De DSO geeft af en toe een losse 503; zonder retry breekt die een hele fase
    af (gemeten 2026-08-01). Zie `src/http_retry.py`.
    """
    def _poging():
        with limiter:
            resp = httpx.get(url, headers=_headers(), params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    return met_retry(_poging, omschrijving=url.rsplit("/", 1)[-1])


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


def find_regelingen_delta(sinds: str | None,
                          doc_types: list[str] | None = None,
                          bronhouder_codes: set[str] | None = None) -> list[dict]:
    """Globale delta-sweep: één lijst-inventarisatie i.p.v. per bronhouder pollen.

    Pagineert de **volledige** `GET /regelingen`-lijst (~10 pagina's van 200) en
    houdt de regelingen over met `geregistreerdMet.tijdstipRegistratie >= sinds`.

    **Waarom de hele lijst en niet stoppen bij de eerste oudere registratie?**
    De eerdere implementatie vroeg `_sort=-registratietijdstip` en brak af bij
    het eerste item ouder dan `sinds` ("nieuwste eerst, dus de rest is ouder").
    Die aanname is fout: de lijst is **niet strikt gesorteerd** — gemeten
    2026-08-01 stond op positie 2 van pagina 1 een registratie uit 2024, tussen
    twee registraties van eind juli in. De sweep stopte daardoor na één item en
    miste stilzwijgend alle latere nieuwe regelingen (16 achterstand over ruim
    een maand, terwijl de sync "0 fouten" rapporteerde). Zie vault `gaps.md`
    G-98.

    De volledige sweep kost ~10 API-calls — minder dan de 381 calls van de
    per-bronhouder-sweep — dus er is geen reden meer om op sortering te
    vertrouwen. `_sort` wordt bewust niet meer meegestuurd: de sorteervolgorde
    is niet betrouwbaar en een instabiele sortering over paginagrenzen kan
    items dubbel opleveren of overslaan.

    `sinds`            — ISO-8601 UTC-tijdstip (bv. "2026-07-15T00:00:00Z");
                         None = geen ondergrens (volledige inventarisatie).
    `doc_types`        — optioneel filter op regeling-type.
    `bronhouder_codes` — optioneel: alleen regelingen van deze overheid-codes
                         (bv. de geconfigureerde 381 bronhouders), zodat de
                         scope gelijk blijft aan de per-bronhouder-sweep en er
                         geen ongewenste landelijke regelingen instromen.

    Returns list of regeling-dicts (zelfde velden als `find_regelingen`) plus
    `bronhouder_code`, `bronhouder_naam` en `tijdstipRegistratie`.

    Let op: `registratietijdstip` is de naam van de sorteersleutel, maar in het
    antwoord heet het veld `geregistreerdMet.tijdstipRegistratie` — niet
    verwarren.

    Let op 2: dit levert alleen wat de DSO *heeft*. Wat wij hebben en de DSO
    niet meer toont (intrekkingen/verdwenen regelingen) volgt hier niet uit;
    daarvoor is de omgekeerde diff nodig — zie `scripts/preview_sync.py` en
    `gaps.md` G-91.
    """
    # per work (identificatie) de nieuwst geregistreerde expressie; de lijst is
    # niet gesorteerd, dus "eerste die we zien" is géén veilige keuze meer.
    per_work: dict[str, dict] = {}
    n_totaal = 0
    console.print(f"  [bold]Delta-sweep[/bold] (volledige lijst, sinds {sinds or 'begin'})...")
    for page in range(1, 1000):
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen",
                    params={"page": page, "size": 200})
        regs = data.get("_embedded", {}).get("regelingen", [])
        if not regs:
            break
        n_totaal += len(regs)
        for reg in regs:
            ts = (reg.get("geregistreerdMet") or {}).get("tijdstipRegistratie", "") or ""
            # Geen vroegtijdige stop: filteren, niet afbreken. Een item zonder
            # tijdstip laten we door (liever een overbodige skip-guard-hit dan
            # een gemiste regeling).
            if sinds and ts and ts < sinds:
                continue
            gg = reg.get("aangeleverdDoorEen", {}) or {}
            code = gg.get("code", "")
            if bronhouder_codes is not None and code not in bronhouder_codes:
                continue
            reg_type = reg.get("type", {}).get("waarde", "")
            if doc_types and reg_type not in doc_types:
                continue
            ident = reg["identificatie"]
            bestaand = per_work.get(ident)
            if bestaand and bestaand["tijdstipRegistratie"] >= ts:
                continue
            per_work[ident] = {
                "identificatie": ident,
                "titel": reg.get("officieleTitel", ""),
                "type": reg_type,
                "expressionId": reg.get("expressionId", ""),
                "aangeleverdDoorEen": gg,
                "bronhouder_code": code,
                "bronhouder_naam": gg.get("naam", "") or code,
                "tijdstipRegistratie": ts,
            }
        if not data.get("_links", {}).get("next", {}).get("href"):
            break
    results = list(per_work.values())
    console.print(f"  [green]Delta: {len(results)} regelingen geregistreerd sinds "
                  f"{sinds or 'begin'}[/green] (uit {n_totaal} in de DSO-lijst)")
    return results


def load_delta(sinds: str | None,
               bronhouder_map: dict[str, tuple[str, str]] | None = None,
               doc_types: list[str] | None = None,
               force: bool = False,
               uitstel_subdiv: bool = False,
               gewijzigd: set[str] | None = None) -> dict[str, str]:
    """Incrementele p2p-sync via één globale registratietijdstip-delta-sweep.

    Vervangt het per-bronhouder pollen: haalt de nieuw-geregistreerde
    regelingen sinds `sinds` op, groepeert ze per bronhouder en laadt per
    bronhouder alleen die subset (de skip-guard in `load_via_api` blijft als
    vangnet). Zie [[Incrementele p2p-sync via registratietijdstip-delta]].

    `bronhouder_map` — overheid_code → (bronhouder_code, naam), zodat de scope
                       en de interne codes gelijk blijven aan de reguliere
                       sweep. None = neem de code/naam uit de API-response.

    Returns dict {bronhouder_code: 'ok'|'error: ...'}.
    """
    codes = set(bronhouder_map) if bronhouder_map else None
    regelingen = find_regelingen_delta(sinds, doc_types, bronhouder_codes=codes)

    per_bh: dict[str, list[dict]] = {}
    for r in regelingen:
        per_bh.setdefault(r["bronhouder_code"], []).append(r)

    results: dict[str, str] = {}
    total = len(per_bh)
    for i, (overheid_code, regs) in enumerate(per_bh.items(), 1):
        if bronhouder_map and overheid_code in bronhouder_map:
            bronhouder_code, naam = bronhouder_map[overheid_code]
        else:
            bronhouder_code, naam = overheid_code, regs[0]["bronhouder_naam"]
        console.rule(f"[bold]delta {i}/{total}[/bold] {naam} ({overheid_code}) — {len(regs)} nieuw")
        try:
            load_via_api(overheid_code, naam, bronhouder_code=bronhouder_code,
                         doc_types=doc_types, force=force, regelingen=regs,
                         uitstel_subdiv=uitstel_subdiv, gewijzigd=gewijzigd)
            results[bronhouder_code] = "ok"
        except Exception as e:
            console.print(f"[red]delta fout {bronhouder_code}: {e}[/red]")
            results[bronhouder_code] = f"error: {e}"
    return results


# ── Documentstructuur ────────────────────────────────────────────────

_INNER_TAG_RE = re.compile(r"<[^>]+>")


def _extract_kop_field(kop_xml: str, tag: str) -> str | None:
    """Pak de inhoud van één Kop-veld (bv. Nummer, Opschrift) als platte tekst.

    DOTALL: STOP-bronnen formatteren `<Kop>` soms compact (alles op één regel)
    en soms ingesprongen met newlines. Zonder DOTALL faalt de match op het
    tweede geval geruisloos en raakten we de waarde kwijt — wat te zien was
    als willekeurig ontbrekende opschriften per artikel.

    Inner-tag-strip: `<Opschrift>` mag mixed content bevatten (`<Inline>`,
    `<Nadruk>`, …); we willen alleen de leesbare tekst overhouden.
    """
    m = re.search(rf"<{tag}>(.*?)</{tag}>", kop_xml, re.DOTALL)
    if not m:
        return None
    text = _INNER_TAG_RE.sub("", m.group(1))
    text = " ".join(text.split())  # whitespace-normalisatie incl. newlines
    return text or None


def _parse_kop(kop_xml: str | None) -> tuple[str | None, str | None]:
    """Extract Nummer and Opschrift from STOP <Kop> XML snippet."""
    if not kop_xml:
        return None, None
    return _extract_kop_field(kop_xml, "Nummer"), _extract_kop_field(kop_xml, "Opschrift")


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
    encoded = _encode_regeling_uri(regeling_uri)
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/documentstructuur")

    top_components = data.get("_embedded", {}).get("documentComponenten", [])
    elements = _flatten_components(top_components)

    if not elements:
        return 0

    from src.loaders.inline_referentie import (
        extract_inline_referenties,
        insert_inline_referenties,
        resolve_target_soort,
    )

    with conn.cursor() as cur:
        eid_to_id = {}
        for elem in elements:
            cur.execute(
                """INSERT INTO p2p.tekst_element
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
                    "UPDATE p2p.tekst_element SET parent_id = %s WHERE id = %s",
                    (eid_to_id[elem["parent_eid"]], eid_to_id[elem["eid"]]),
                )

    for elem in elements:
        te_id = eid_to_id.get(elem["eid"])
        if te_id is None:
            continue
        refs = extract_inline_referenties(elem["inhoud"])
        if refs:
            insert_inline_referenties(conn, te_id, refs)

    # GIO's vóór de resolve, niet erna. Pass A matcht ExtIoRef.@ref op
    # p2p.geo_informatieobject.frbr_expression; staat die tabel nog leeg voor
    # deze regeling, dan vindt hij niets en blijft de verwijzing dood. Dat was
    # jarenlang het geval omdat gio_zip alleen aan losse backfill-scripts hing:
    # 17.582 dode verwijzingen over 125 regelingen (vault gaps.md G-119).
    #
    # De ZIP is gecached per work, en de Download API levert toch altijd de
    # vigerende versie — voor een regeling die we zojuist als vigerend hebben
    # geladen is dat precies de goede. Best-effort: een mislukte GIO-stap mag
    # het laden van de tekst niet ongedaan maken, dus de fout wordt gelogd en
    # de resolve draait alsnog (die koppelt dan wat er al ligt).
    try:
        from src.loaders.gio_zip import process_zip
        from src.loaders.ow_loader import _download_regeling
        zip_path = _download_regeling(regeling_uri)   # regeling_uri is het work
        if zip_path:
            process_zip(zip_path, conn, regeling_expression=expression_id)
    except Exception as e:
        console.print(f"    [yellow]GIO-stap overgeslagen: {str(e)[:120]}[/yellow]")

    resolve_target_soort(conn, regeling_expression=expression_id)

    conn.commit()
    return len(elements)


# ── Annotaties (artikelstructuur) ────────────────────────────────────

# Waardelijst-labels die de Presenteren-API in `waardeInRegeltekst` zet. Het
# veld heet als een boolean en de kolom IS er een, maar de API levert een
# waardelijst-label: gemeten 2026-09-05 op de Zuid-Hollandse Omgevingsverordening
# 12x null en 1x de string "waarde staat in regeltekst".
#
# Die ene rij kostte de HELE annotatie-load van die verordening: de INSERT klapte
# op `invalid input syntax for type boolean`, en de `except` om het annotatieblok
# brak daarmee alles af. Gevolg: de vigerende ZHOV stond op 2.578 tekstelementen
# en 0 juridische regels, terwijl de verdrongen versie er 914 had — en de sync
# meldde "0 fouten". Zie vault G-144.
_WIR_WAAR = {"waarde staat in regeltekst"}
_WIR_ONWAAR: set[str] = set()   # nog geen tegenhanger waargenomen


def _waarde_in_regeltekst(waarde):
    """Maak van `waardeInRegeltekst` een boolean of None — nooit een exception.

    Accepteert de vormen die de API in de praktijk levert: een echte boolean,
    None, een waardelijst-object (`{"waarde": ...}`, zoals `type`/`eenheid`/
    `groep` verderop) en een kaal label. Een onbekend label wordt None én
    gemeld, zodat een nieuwe waardelijst-waarde zichtbaar wordt in plaats van
    een hele regeling te kosten.
    """
    if waarde is None or isinstance(waarde, bool):
        return waarde
    if isinstance(waarde, dict):
        waarde = waarde.get("waarde")
        if waarde is None or isinstance(waarde, bool):
            return waarde
    tekst = str(waarde).strip().lower()
    if tekst in _WIR_WAAR:
        return True
    if tekst in _WIR_ONWAAR:
        return False
    console.print(f"    [yellow]onbekende waardeInRegeltekst: {waarde!r} "
                  f"— als NULL opgeslagen[/yellow]")
    return None


def load_regeltekstannotaties(conn, regeling_uri: str, bronhouder: str,
                              expression_id: str | None = None):
    """Load regeltekstannotaties (artikelstructuur) via Presenteren API."""
    encoded = _encode_regeling_uri(regeling_uri)
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
                    """INSERT INTO p2p.locatie
                         (identificatie, locatie_type, noemer, geometrie, geometrie_identificatie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992), %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992),
                         geometrie_identificatie = COALESCE(EXCLUDED.geometrie_identificatie, p2p.locatie.geometrie_identificatie)""",
                    (loc_id, loc_type, noemer, json.dumps(geojson), geom_id, json.dumps(geojson)),
                )
                stats["geometrieen"] += 1
            else:
                cur.execute(
                    """INSERT INTO p2p.locatie
                         (identificatie, locatie_type, noemer, geometrie, geometrie_identificatie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992), %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie_identificatie = COALESCE(EXCLUDED.geometrie_identificatie, p2p.locatie.geometrie_identificatie)""",
                    (loc_id, loc_type, noemer, geom_id),
                )
            stats["locaties"] += 1

        # ── Activiteiten ──
        for act in data.get("activiteiten", []):
            cur.execute(
                """INSERT INTO p2p.activiteit (identificatie, naam, groep, is_tophaak)
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
                    """UPDATE p2p.activiteit SET bovenliggende = %s
                       WHERE identificatie = %s
                       AND EXISTS (SELECT 1 FROM p2p.activiteit WHERE identificatie = %s)""",
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
                    """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            cur.execute(
                """INSERT INTO p2p.gebiedsaanwijzing (identificatie, type, naam, groep, locatie_id)
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

                # Themas (array; in DSO API soms list[str] van URIs, soms list[dict{"waarde": str}])
                themas_raw = regel.get("themas") or regel.get("thema") or []
                themas: list[str] = []
                for t in themas_raw:
                    val = t.get("waarde") if isinstance(t, dict) else t
                    if val:
                        tail = val.rstrip("/").split("/")[-1]
                        if tail:
                            themas.append(tail)

                # Instructieregel-specifiek. LET OP: de DSO Presenteren-API levert deze
                # velden MEERVOUDIG (arrays): instructieregelInstrumenten /
                # instructieregelTaakuitoefeningen (IMOW 0..*). We nemen de concept-tail
                # van `code` (Title-case, consistent met het XML-pad in ow_xml.py).
                def _concept_tails(items):
                    tails = []
                    for it in items or []:
                        uri = it.get("code") or it.get("waarde") if isinstance(it, dict) else it
                        if uri:
                            tail = uri.rstrip("/").split("/")[-1]
                            if tail:
                                tails.append(tail)
                    return tails or None

                instr_instr = None
                instr_taak = None
                if regel_type == "Instructieregel":
                    instr_instr = _concept_tails(regel.get("instructieregelInstrumenten"))
                    instr_taak = _concept_tails(regel.get("instructieregelTaakuitoefeningen"))

                cur.execute(
                    """INSERT INTO p2p.juridische_regel
                       (identificatie, regel_type, idealisatie, regeltekst_wid,
                        thema, instructieregel_instrument, instructieregel_taakuitoefening,
                        regeling_expression)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                           thema = COALESCE(EXCLUDED.thema, p2p.juridische_regel.thema),
                           instructieregel_instrument = COALESCE(EXCLUDED.instructieregel_instrument, p2p.juridische_regel.instructieregel_instrument),
                           instructieregel_taakuitoefening = COALESCE(EXCLUDED.instructieregel_taakuitoefening, p2p.juridische_regel.instructieregel_taakuitoefening),
                           regeling_expression = COALESCE(EXCLUDED.regeling_expression, p2p.juridische_regel.regeling_expression),
                           -- Volg de nieuwe versie: regel-ID's zijn stabiel over versies, maar het
                           -- artikel-wId verandert. Zonder deze regel blijft de regel op het oude
                           -- artikel-wId hangen → viewer-drilldown op de nieuwste expressie mist 'm.
                           regeltekst_wid = EXCLUDED.regeltekst_wid
                       """,
                    (regel["identificatie"], regel_type, idealisatie, regeltekst_wid,
                     themas or None, instr_instr, instr_taak, expression_id),
                )
                stats["regels"] += 1

                # GA-junction
                for ga_ref in regel.get("gebiedsaanwijzingRefs", []):
                    cur.execute(
                        """INSERT INTO p2p.juridische_regel_gebiedsaanwijzing
                           (juridische_regel_id, gebiedsaanwijzing_id)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (regel["identificatie"], ga_ref),
                    )

                # ALA's (activiteit-gebonden locatie-koppeling)
                alas = regel.get("activiteitLocatieaanduidingen", [])
                for ala in alas:
                    act_ref = ala.get("activiteitRef", "")
                    kwal = ala.get("activiteitregelkwalificatie", {})
                    kwal_val = kwal.get("waarde") if isinstance(kwal, dict) else kwal
                    for loc_ref in ala.get("locatieRefs", []):
                        cur.execute(
                            """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT (identificatie) DO NOTHING""",
                            (loc_ref,),
                        )
                        cur.execute(
                            """INSERT INTO p2p.activiteit_locatieaanduiding
                               (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT DO NOTHING""",
                            (regel["identificatie"], act_ref, loc_ref, kwal_val),
                        )
                        stats["ala"] += cur.rowcount

                # Fallback: directe locatieRefs op regel-niveau (geen activiteit).
                # Komt voor bij Instructieregel/Omgevingswaardegel en bij RvI's van
                # rijksregelingen (Bal/Bkl/Bbl/Ob) waar de regel een ambtsgebied- of
                # regelingsgebied-werking heeft zonder activiteit-koppeling. Zonder
                # deze fallback zijn die regels onvindbaar via coord-queries.
                # Alleen toepassen wanneer er geen activiteitLocatieaanduidingen zijn,
                # om dubbele inserts voor activiteit-jr's te voorkomen.
                if not alas:
                    for loc_ref in regel.get("locatieRefs", []):
                        cur.execute(
                            """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT (identificatie) DO NOTHING""",
                            (loc_ref,),
                        )
                        cur.execute(
                            """INSERT INTO p2p.activiteit_locatieaanduiding
                               (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                               VALUES (%s, NULL, %s, NULL)
                               ON CONFLICT DO NOTHING""",
                            (regel["identificatie"], loc_ref),
                        )
                        stats["ala"] += cur.rowcount

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
                    """INSERT INTO p2p.norm
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
                            """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                               VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                               ON CONFLICT (identificatie) DO NOTHING""",
                            (nw_loc,),
                        )
                        cur.execute(
                            """INSERT INTO p2p.normwaarde
                               (norm_id, locatie_id, kwalitatieve_waarde, kwantitatieve_waarde, waarde_in_regeltekst)
                               VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT DO NOTHING""",
                            (norm["identificatie"], nw_loc,
                             nw.get("kwalitatieveWaarde"),
                             nw.get("kwantitatieveWaarde"),
                             _waarde_in_regeltekst(nw.get("waardeInRegeltekst"))),
                        )
                        stats["normwaarden"] += cur.rowcount

        # ── Juridische regel → norm junctions ──
        for regel_type_key in ("regelsVoorIedereen", "instructieregels", "omgevingswaarderegels"):
            norm_ref_key = "omgevingswaardeRefs" if regel_type_key == "omgevingswaarderegels" else "omgevingsnormRefs"
            for regel in data.get(regel_type_key, []):
                for norm_ref in regel.get(norm_ref_key, []):
                    cur.execute(
                        """INSERT INTO p2p.juridische_regel_norm
                           (juridische_regel_id, norm_id)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (regel["identificatie"], norm_ref),
                    )

        # ── Kaarten ──
        for kaart in data.get("kaarten", []):
            cur.execute(
                """INSERT INTO p2p.kaart (identificatie, naam)
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
                    """INSERT INTO p2p.kaartlaag (kaart_id, naam, gebiedsaanwijzing_id, norm_id, activiteit_id)
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
    encoded = _encode_regeling_uri(regeling_uri)
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}/divisieannotaties",
                params={"locatieSelectie": "primair"})

    stats = {"tekstdelen": 0, "locaties": 0, "geometrieen": 0, "ga": 0,
             "hoofdlijnen": 0, "kaarten": 0, "kaartlagen": 0,
             "td_hoofdlijn": 0, "td_gebiedsaanwijzing": 0,
             "td_ref_zonder_doel": 0}

    with conn.cursor() as cur:
        # ── Locaties ──
        for loc in data.get("locaties", []):
            loc_id = loc["identificatie"]
            geom_id = loc.get("geometrieIdentificatie")

            geojson = _get_geometry(geom_id) if geom_id else None
            if geojson:
                cur.execute(
                    """INSERT INTO p2p.locatie
                         (identificatie, locatie_type, noemer, geometrie, geometrie_identificatie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992), %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992),
                         geometrie_identificatie = COALESCE(EXCLUDED.geometrie_identificatie, p2p.locatie.geometrie_identificatie)""",
                    (loc_id, loc["locatieType"], loc.get("noemer"),
                     json.dumps(geojson), geom_id, json.dumps(geojson)),
                )
                stats["geometrieen"] += 1
            else:
                cur.execute(
                    """INSERT INTO p2p.locatie
                         (identificatie, locatie_type, noemer, geometrie, geometrie_identificatie)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992), %s)
                       ON CONFLICT (identificatie) DO UPDATE SET
                         geometrie_identificatie = COALESCE(EXCLUDED.geometrie_identificatie, p2p.locatie.geometrie_identificatie)""",
                    (loc_id, loc["locatieType"], loc.get("noemer"), geom_id),
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
                    """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            cur.execute(
                """INSERT INTO p2p.gebiedsaanwijzing (identificatie, type, naam, groep, locatie_id)
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
                    """INSERT INTO p2p.locatie (identificatie, locatie_type, geometrie)
                       VALUES (%s, 'Onbekend', ST_SetSRID(ST_MakePoint(0, 0), 28992))
                       ON CONFLICT (identificatie) DO NOTHING""",
                    (loc_id,),
                )
            themas = None
            if td.get("themas"):
                themas = [t.get("waarde", t) if isinstance(t, dict) else t for t in td["themas"]]

            cur.execute(
                """INSERT INTO p2p.tekstdeel (identificatie, divisie_wid, thema, locatie_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (td["identificatie"], td.get("divisietekstRef", ""), themas, loc_id),
            )
            stats["tekstdelen"] += 1

        # ── Hoofdlijnen ──
        for hl in data.get("hoofdlijnen", []):
            cur.execute(
                """INSERT INTO p2p.hoofdlijn (identificatie, soort, naam)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (hl["identificatie"], hl.get("soort", ""), hl.get("naam", "")),
            )
            stats["hoofdlijnen"] += 1

        # ── Kaarten ──
        for kaart in data.get("kaarten", []):
            cur.execute(
                """INSERT INTO p2p.kaart (identificatie, naam)
                   VALUES (%s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (kaart["identificatie"], kaart.get("naam", "")),
            )
            stats["kaarten"] += 1

            for kl in kaart.get("kaartlagen", []):
                ga_refs = kl.get("gebiedsaanwijzingRefs", [])
                cur.execute(
                    """INSERT INTO p2p.kaartlaag (kaart_id, naam, gebiedsaanwijzing_id)
                       VALUES (%s, %s, %s)""",
                    (kaart["identificatie"],
                     kl.get("naam", ""),
                     ga_refs[0] if ga_refs else None),
                )
                stats["kaartlagen"] += 1

        # ── Koppelingen tekstdeel → hoofdlijn / gebiedsaanwijzing ──
        #
        # Bewust hier, ná alle objecten: beide junctions hebben een FK naar een
        # tabel die hierboven pas gevuld wordt. Tot 2026-08-09 werden ze
        # helemaal niet geschreven, hoewel de API ze wel levert
        # (`hoofdlijnRefs`, `gebiedsaanwijzingRefs` op elk tekstdeel). Gevolg:
        # `tekstdeel_hoofdlijn` stond landelijk op 0 rijen en 40% van de
        # gebiedsaanwijzingen leek nergens aan te hangen — vault gaps.md G-124.
        #
        # De `WHERE EXISTS` slaat een verwijzing over waarvan het doel niet in
        # deze respons zat, in plaats van de hele transactie te laten klappen.
        # Overgeslagen refs worden geteld, niet verzwegen.
        for td in data.get("tekstdelen", []):
            for hl_ref in td.get("hoofdlijnRefs", []):
                cur.execute(
                    """INSERT INTO p2p.tekstdeel_hoofdlijn (tekstdeel_id, hoofdlijn_id)
                       SELECT %s, %s
                        WHERE EXISTS (SELECT 1 FROM p2p.hoofdlijn WHERE identificatie = %s)
                       ON CONFLICT DO NOTHING""",
                    (td["identificatie"], hl_ref, hl_ref),
                )
                stats["td_hoofdlijn" if cur.rowcount else "td_ref_zonder_doel"] += 1
            for ga_ref in td.get("gebiedsaanwijzingRefs", []):
                cur.execute(
                    """INSERT INTO p2p.tekstdeel_gebiedsaanwijzing
                           (tekstdeel_id, gebiedsaanwijzing_id)
                       SELECT %s, %s
                        WHERE EXISTS (SELECT 1 FROM p2p.gebiedsaanwijzing
                                       WHERE identificatie = %s)
                       ON CONFLICT DO NOTHING""",
                    (td["identificatie"], ga_ref, ga_ref),
                )
                stats["td_gebiedsaanwijzing" if cur.rowcount else "td_ref_zonder_doel"] += 1

    conn.commit()
    return stats


# ── Pons + Regelingsgebied (via _expand) ────────────────────────────

def _upsert_locatie_met_kinderen(cur, loc: dict, default_type: str) -> None:
    """Sla een locatie op met geometrie.

    DSO retourneert Gebiedengroepen (typisch voor Aanwijzingsbesluit N2000,
    Projectbesluit, Toegangsbeperkingsbesluit) zonder eigen
    `geometrieIdentificatie`; de geometrie zit dan een laag dieper in
    `_embedded.omvat[].geometrieIdentificatie`. We slaan elke child-locatie
    apart op én aggregaten met ST_Union voor de parent.
    """
    loc_id = loc["identificatie"]
    locatie_type = loc.get("locatieType", default_type)
    noemer = loc.get("noemer")

    geom_id = loc.get("geometrieIdentificatie")
    if geom_id:
        geojson = _get_geometry(geom_id)
        if geojson:
            cur.execute(
                """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
                   VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
                   ON CONFLICT (identificatie) DO UPDATE SET
                     geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
                (loc_id, locatie_type, noemer, json.dumps(geojson), json.dumps(geojson)),
            )
            return

    children = loc.get("_embedded", {}).get("omvat", [])
    geladen_kinderen: list[str] = []
    for child in children:
        child_id = child.get("identificatie")
        child_geom_id = child.get("geometrieIdentificatie")
        if not child_id or not child_geom_id:
            continue
        geojson = _get_geometry(child_geom_id)
        if not geojson:
            continue
        cur.execute(
            """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
               VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992))
               ON CONFLICT (identificatie) DO UPDATE SET
                 geometrie = ST_SetSRID(ST_GeomFromGeoJSON(%s), 28992)""",
            (child_id, child.get("locatieType", "Gebied"), child.get("noemer"),
             json.dumps(geojson), json.dumps(geojson)),
        )
        geladen_kinderen.append(child_id)

    if geladen_kinderen:
        cur.execute(
            """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
               SELECT %s, %s, %s, ST_Union(geometrie)
               FROM p2p.locatie WHERE identificatie = ANY(%s)
               ON CONFLICT (identificatie) DO UPDATE SET geometrie = EXCLUDED.geometrie""",
            (loc_id, locatie_type, noemer, geladen_kinderen),
        )
        return

    cur.execute(
        """INSERT INTO p2p.locatie (identificatie, locatie_type, noemer, geometrie)
           VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(0, 0), 28992))
           ON CONFLICT (identificatie) DO NOTHING""",
        (loc_id, locatie_type, noemer),
    )


def load_regeling_expand(conn, regeling_uri: str, expression_id: str):
    """Load pons and regelingsgebied via GET /regelingen/{id}?_expand=true.

    locatieSelectie=primair laat de API per Gebiedengroep (Pons,
    Regelingsgebied bij N2000/PB) een samengestelde geometrieIdentificatie
    terugleveren in plaats van enkel referenties naar secundaire leden.
    Zonder deze parameter zou _upsert_locatie_met_kinderen terugvallen op
    de POINT(0,0)-placeholder omdat noch `geometrieIdentificatie` noch
    `_embedded.omvat[]` populated zijn. Zie analyse [[Ponsenkaart.nl
    databehoefte uit OCD]] en gap G-73.
    """
    encoded = _encode_regeling_uri(regeling_uri)
    data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}",
                params={"_expand": "true", "locatieSelectie": "primair"})

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
                "UPDATE p2p.regeling SET is_tijdelijkdeel_van = %s WHERE frbr_expression = %s",
                (tdv_work, expression_id),
            )

        # ── Regelingsgebied ──
        rg = embedded.get("regelingsgebied")
        if rg:
            _upsert_locatie_met_kinderen(cur, rg, default_type="Regelingsgebied")
            cur.execute(
                "UPDATE p2p.regeling SET regelingsgebied_id = %s WHERE frbr_expression = %s",
                (rg["identificatie"], expression_id),
            )
            stats["regelingsgebied"] = True

        # ── Pons ──
        pons = embedded.get("pons")
        if pons:
            _upsert_locatie_met_kinderen(cur, pons, default_type="Pons")
            cur.execute(
                """INSERT INTO p2p.pons (identificatie, locatie_id)
                   VALUES (%s, %s)
                   ON CONFLICT (identificatie) DO NOTHING""",
                (pons["identificatie"], pons["identificatie"]),
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


def herlaad_annotaties(expr_ids: list[str]) -> int:
    """Herlaad alleen de annotatie-stap voor een lijst regeling-expressies.

    Gebruikt voor remediatie: her-annoteren corrigeert o.a. stale
    regeltekst_wid (het annotatie-endpoint geeft de nieuwe wIds, de ON CONFLICT
    volgt nu mee). Zoekt work/type/bronhouder op in p2p.regeling en draait de
    juiste annotatie-loader. Returnt het aantal succesvol geannoteerde regelingen.
    """
    conn = get_conn()
    n = 0
    try:
        for expr in expr_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT frbr_work, documenttype, bronhouder "
                    "FROM p2p.regeling WHERE frbr_expression = %s", (expr,))
                row = cur.fetchone()
            if not row:
                console.print(f"  [yellow]{expr}: niet in p2p.regeling[/yellow]")
                continue
            work, doc_type, bh = row["frbr_work"], row["documenttype"], row["bronhouder"]
            console.print(f"  [bold]Herannoteren:[/bold] {expr.split('/')[-1]} "
                          f"({doc_type}, {bh})")
            try:
                if doc_type in VRIJETEKST_TYPES:
                    load_divisieannotaties(conn, work, bh)
                else:
                    load_regeltekstannotaties(conn, work, bh, expr)
                conn.commit()
                n += 1
            except Exception as e:
                conn.rollback()
                console.print(f"    [red]mislukt: {e}[/red]")
        return n
    finally:
        conn.close()


# ── Main entry point ─────────────────────────────────────────────────

def load_via_api(overheid_code: str, naam: str,
                 bronhouder_code: str | None = None,
                 doc_types: list[str] | None = None,
                 force: bool = False,
                 regelingen: list[dict] | None = None,
                 uitstel_subdiv: bool = False,
                 gewijzigd: set[str] | None = None):
    """Load all regelingen for an overheid via API pipeline.

    Een FRBR-expressie is immutabel; expressies waarvoor al tekst-elementen
    bestaan worden overgeslagen tenzij force=True.

    `regelingen` — optioneel een vooraf opgehaalde regeling-lijst (zelfde vorm
    als `find_regelingen` levert). Wordt gebruikt door de delta-sweep
    (`load_delta`) om de per-bronhouder `find_regelingen`-poll over te slaan;
    None = zelf ophalen via `find_regelingen`.

    `uitstel_subdiv` — de `locatie_subdiv`-herbouw NIET hier draaien maar de
    bronhouder aanmelden in `gewijzigd`, zodat de sync hem gebundeld in de
    post-fase kan doen ("harvest eerst, rekenen later"). Default False, zodat
    losse aanroepen (CLI, herstelscripts) zichzelf blijven bedruipen.
    `gewijzigd` — set die met de bronhouder-codes gevuld wordt die daadwerkelijk
    iets geladen hebben.

    Returns het aantal nieuw geladen regelingen.
    """
    if bronhouder_code is None:
        bronhouder_code = overheid_code

    from src.db import normalize_bronhouder_code
    bronhouder_code = normalize_bronhouder_code(bronhouder_code)

    conn = get_conn()
    try:
        if regelingen is None:
            regelingen = find_regelingen(overheid_code, naam, doc_types)

        with conn.cursor() as cur:
            upsert_bronhouder(cur, bronhouder_code, naam)
        conn.commit()

        n_skipped = 0
        for reg in regelingen:
            regeling_uri = reg["identificatie"]
            expression_id = reg.get("expressionId", regeling_uri)
            doc_type = reg["type"]
            regelingmodel = REGELINGMODEL_MAP.get(doc_type, "RegelingCompact")

            if not force:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM p2p.tekst_element WHERE regeling_expression = %s LIMIT 1",
                        (expression_id,))
                    already_loaded = cur.fetchone() is not None
                conn.commit()
                if already_loaded:
                    n_skipped += 1
                    console.print(f"  [dim]Skip (al geladen): {reg['titel'][:60]}[/dim]")
                    continue

            console.print(f"\n  [bold]Loading: {reg['titel'][:60]}[/bold] ({doc_type})")

            # ── Regeling metadata ──
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO p2p.regeling
                       (frbr_expression, frbr_work, regelingmodel, opschrift, citeertitel, bronhouder, documenttype)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (frbr_expression) DO NOTHING""",
                    (expression_id, regeling_uri, regelingmodel,
                     reg.get("titel", ""), reg.get("titel", ""),
                     bronhouder_code, doc_type),
                )
                # Zojuist geladen = vigerende versie → oudere expressies van
                # hetzelfde work standaard verbergen (verouderde-versie).
                markeer_siblings_inactief(cur, regeling_uri, expression_id)
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
                    stats = load_regeltekstannotaties(conn, regeling_uri, bronhouder_code, expression_id)
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
                    stats = load_regeltekstannotaties(conn, regeling_uri, bronhouder_code, expression_id)
            except Exception as e:
                # Niet alleen printen. Deze `except` dekt de hele annotatielaag
                # van een regeling — juridische regels, activiteiten, normen,
                # gebiedsaanwijzingen — en tot 2026-09-05 kwam een mislukking
                # hier nergens in de foutentelling terecht. De sync meldde dan
                # "0 fouten" terwijl een provinciale omgevingsverordening zonder
                # één regel in de database stond. Zelfde soort stilte als G-98.
                console.print(f"    [red]Annotaties failed: {e}[/red]")
                ANNOTATIE_FOUTEN.append((expression_id or regeling_uri, str(e)[:200]))

        # Afgeleide subdiv-tabel alléén bijwerken als er ook echt iets geladen
        # is. De ST_Subdivide-herbouw is zwaar (duizenden stukjes per bronhouder);
        # onvoorwaardelijk draaien betekende dat een volledig-skip sweep alsnog
        # ~2M geometrie-stukjes over 381 bronhouders herberekende — de oorzaak
        # van de p2p-regressie van minuten naar ~3 uur. Bij niets-geladen zijn de
        # locaties ongewijzigd, dus is de subdiv-tabel al actueel.
        n_geladen = len(regelingen) - n_skipped
        if n_geladen > 0 or force:
            if uitstel_subdiv:
                # Harvest eerst, rekenen later: de herbouw gaat gebundeld naar de
                # post-fase, zodat het API-venster niet openblijft voor rekenwerk.
                if gewijzigd is not None:
                    gewijzigd.add(bronhouder_code)
                console.print("  [dim]locatie_subdiv: uitgesteld naar de post-fase[/dim]")
            else:
                from src.loaders.subdiv import refresh_locatie_subdiv
                n_sub = refresh_locatie_subdiv(conn, bronhouder_code)
                console.print(f"  locatie_subdiv ververst: {n_sub} stukjes")
        else:
            console.print("  [dim]locatie_subdiv: overgeslagen (niets nieuw geladen)[/dim]")

        console.print(f"\n[green]Done: {n_geladen} nieuw geladen, "
                      f"{n_skipped} overgeslagen (al geladen) voor {naam}[/green]")
        return n_geladen

    finally:
        conn.close()
