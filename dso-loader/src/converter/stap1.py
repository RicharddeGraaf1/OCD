"""Stap 1: Mechanische conversie wro → conv.

Converteert een bestemmingsplan (wro.ruimtelijk_instrument) naar
Ow-structuur in het conv-schema. Puur SQL-transformaties, geen LLM.

Levert op:
  - conv.regeling (1 rij)
  - conv.tekst_element (artikelboom met gegenereerde eId's)
  - conv.locatie + conv.locatiegroep_lid (planobject-geometrieën)
  - conv.gebiedsaanwijzing (afgeleid uit bestemmingshoofdgroep)
  - conv.conversie_meta (metadata)
"""

import os
import re
import sys
import uuid
from collections import defaultdict

import psycopg
from rich.console import Console

from src.db import get_conn

# Fix Windows console encoding
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
console = Console(highlight=False)

# ── Mappingtabel bestemmingshoofdgroep → Ow-functie ──────────────────

# Mapping gevalideerd tegen IMOW waardelijsten v5.1.0:
#   type → waardelijst "type gebiedsaanwijzing" (lowercase)
#   groep → waardelijst "functiegroep" of "beperkingengebiedgroep" (lowercase)
BESTEMMING_NAAR_OW = {
    # enkelbestemmingen → type "functie" + functiegroep
    "agrarisch":                ("functie", "agrarisch"),
    "agrarisch met waarden":    ("functie", "landbouw"),
    "bedrijf":                  ("functie", "bedrijf"),
    "bedrijventerrein":         ("functie", "bedrijventerrein"),
    "bos":                      ("functie", "natuur"),
    "centrum":                  ("functie", "centrumgebied"),
    "cultuur en ontspanning":   ("functie", "cultuur"),
    "detailhandel":             ("functie", "detailhandel"),
    "dienstverlening":          ("functie", "dienstverlening"),
    "gemengd":                  ("functie", "centrumgebied"),
    "groen":                    ("functie", "groen"),
    "horeca":                   ("functie", "horeca"),
    "kantoor":                  ("functie", "kantoor"),
    "maatschappelijk":          ("functie", "maatschappelijk"),
    "natuur":                   ("functie", "natuur"),
    "recreatie":                ("functie", "recreatie"),
    "sport":                    ("functie", "sport"),
    "tuin":                     ("functie", "wonen"),
    "verkeer":                  ("functie", "verkeer"),
    "water":                    ("functie", "water"),
    "wonen":                    ("functie", "wonen"),
    "woongebied":               ("functie", "woongebied"),
    "overig":                   ("functie", "overig"),
    # dubbelbestemmingen → type "beperkingengebied" + beperkingengebiedgroep,
    # of type "functie" + functiegroep
    "leiding":                  ("beperkingengebied", "leiding"),
    "waarde":                   ("functie", "waarde"),
    "waterstaat":               ("beperkingengebied", "waterstaatswerk"),
}


def _uid(prefix: str, bronhouder: str) -> str:
    """Genereer een NEN3610-achtig ID."""
    return f"nl.imow-gm{bronhouder}.{prefix}.conv-{uuid.uuid4().hex[:12]}"


def _derive_chapter_name(slug: str, child_rows: list[dict]) -> str:
    """Leidt een leesbare hoofdstuknaam af uit de slug en kind-artikelen.

    De slug is de rechtstreekse naam uit de parent_id (bv. "Begrippen",
    "Bouwenenslopen"). Voor korte slugs is die direct bruikbaar. Voor
    lange aaneengeschreven slugs zoeken we een beter alternatief in de
    kind-artikelen.
    """
    # Stap 1: CamelCase splitsen (werkt voor "BouwEnSlopen")
    camel_split = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", slug)
    if camel_split != slug:
        return camel_split[0].upper() + camel_split[1:]

    # Stap 2: zoek in kind-artikelen of de slug voorkomt als substring
    # van een label (zonder spaties). Bv. slug "Bouwenenslopen" matcht
    # label "Inleidende bepalingen over het bouwen en het slopen" want
    # "bouwenenslopen" ⊂ "inleidendebepalingenoverhetbouwenenhetslopen"
    slug_lower = slug.lower()
    for child in child_rows:
        label = child.get("label") or child.get("naam") or ""
        stripped = re.sub(r"^(?:Artikel\s+)?\d+(?:\.\d+)*\s*", "", label).strip()
        if len(stripped) < 5:
            continue
        label_collapsed = stripped.lower().replace(" ", "")
        if slug_lower in label_collapsed:
            # Extraheer het overeenkomende stuk MET spaties uit het label
            # Zoek de positie in de collapsed string en map terug
            idx = label_collapsed.index(slug_lower)
            # Reconstrueer: tel chars in de originele string
            orig_idx = 0
            collapsed_count = 0
            while collapsed_count < idx and orig_idx < len(stripped):
                if stripped[orig_idx] != " ":
                    collapsed_count += 1
                orig_idx += 1
            start = orig_idx
            while collapsed_count < idx + len(slug_lower) and orig_idx < len(stripped):
                if stripped[orig_idx] != " ":
                    collapsed_count += 1
                orig_idx += 1
            result = stripped[start:orig_idx].strip()
            if result:
                return result[0].upper() + result[1:]

    # Stap 3: slug as-is. Voor korte enkelvoudige woorden (Begrippen,
    # Functies, Groen) is dit prima. Voor langere aaneengeschreven slugs
    # (Bouwenenslopen, Bouwenopdezelocatiealleenmetvergunning) is het
    # lelijk maar functioneel — de planner hernoemt bij review.
    # TODO: NL-tokenizer (bv. spaCy) zou hier beter splitsen.

    # Stap 4: fallback — capitalize de slug
    return slug[0].upper() + slug[1:] if slug else slug


def _make_eid(niveau: int, nummer: str | None, volgnummer: int,
              parent_eid: str | None) -> str:
    """Genereer STOP-conforme eId op basis van niveau en nummering.

    Met `parent_eid` wordt de eId genest (`{parent}__chp_X` / `__art_X`),
    zodat top-level containers (Lichaam/Bijlage/Toelichting) hoofdstukken
    met identiek nummer in verschillende secties uniek kunnen houden.
    """
    if nummer:
        # Gebruik het bestemmingsplan-nummer voor stabiele eId's
        parts = nummer.replace(".", "_")
        clean = re.sub(r"[^a-zA-Z0-9_]", "", parts)
    else:
        clean = str(volgnummer)

    prefix = "chp" if niveau <= 1 else "art"
    if parent_eid:
        return f"{parent_eid}__{prefix}_{clean}"
    return f"{prefix}_{clean}"


def _element_type_from_niveau(niveau: int, object_type: str) -> str:
    """Map wro-niveau + object_type naar STOP element_type."""
    if niveau <= 1:
        return "Hoofdstuk"
    if object_type.lower() in ("paragraaf",):
        return "Afdeling"
    if niveau == 2:
        return "Artikel"
    if object_type.lower() in ("lid",):
        return "Lid"
    return "Artikel"


# ── Bucket-classificatie: Lichaam / Bijlage / Toelichting ───────────

# STOP-conforme top-level containers. De viewer-frontend filtert op deze
# `element_type`-waarden om de tabs Regels/Bijlagen/Toelichting te vullen.
_BUCKETS = ("Lichaam", "Bijlage", "Toelichting")
_BUCKET_EID = {
    "Lichaam":     "body",
    "Bijlage":     "bijlage",
    "Toelichting": "toelichting",
}


def _classify_root(naam: str | None, object_type: str | None) -> str:
    """Map een wro top-level rij naar bucket Lichaam/Bijlage/Toelichting.

    `object_type` is het primaire signaal (door de IHR-loader uit de titel
    afgeleid). Voor generieke types ('Overig', 'Hoofdstuk') vallen we
    terug op een prefix-match op de naam zelf.
    """
    ot = (object_type or "").lower()
    if ot == "bijlage":
        return "Bijlage"
    if ot == "toelichting":
        return "Toelichting"
    if ot == "regels":
        return "Lichaam"
    n = (naam or "").lower().strip()
    if n.startswith("bijlage"):
        return "Bijlage"
    if n.startswith("toelichting"):
        return "Toelichting"
    return "Lichaam"


# ── Stap 1.1: Regeling ──────────────────────────────────────────────

def convert_regeling(conn: psycopg.Connection, instrument_idn: str) -> str:
    """Maak conv.regeling aan. Returns frbr_expression."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ri.idn, ri.naam, ri.bronhouder, b.naam AS gemeente_naam
            FROM wro.ruimtelijk_instrument ri
            JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
            WHERE ri.idn = %s
        """, (instrument_idn,))
        ri = cur.fetchone()
        if not ri:
            raise ValueError(f"Instrument niet gevonden: {instrument_idn}")

        frbr_work = f"/akn/nl/act/gm{ri['bronhouder']}/conv/{uuid.uuid4().hex[:8]}"
        frbr_expression = f"{frbr_work}/nld@1"
        opschrift = f"Omgevingsplan {ri['gemeente_naam']}, deel {ri['naam']}"

        cur.execute("""
            INSERT INTO conv.regeling (frbr_expression, frbr_work, regelingmodel,
                                       opschrift, bronhouder, documenttype)
            VALUES (%s, %s, 'RegelingKlassiek', %s, %s, 'Omgevingsplan')
            ON CONFLICT (frbr_expression) DO NOTHING
        """, (frbr_expression, frbr_work, opschrift, ri["bronhouder"]))

    return frbr_expression


# ── Stap 1.2: Tekst ─────────────────────────────────────────────────

def convert_tekst(conn: psycopg.Connection, instrument_idn: str,
                  regeling_expression: str) -> int:
    """Converteer wro.wro_tekst_object → conv.tekst_element.

    Reconstrueert virtuele parent-nodes uit parent_id strings en
    genereert STOP-conforme eId's.
    """
    with conn.cursor() as cur:
        # Haal bronhouder op
        cur.execute("SELECT bronhouder FROM wro.ruimtelijk_instrument WHERE idn = %s",
                    (instrument_idn,))
        bronhouder = cur.fetchone()["bronhouder"]

        # Haal alle tekst-objecten op
        cur.execute("""
            SELECT identificatie, volgnummer, niveau, object_type,
                   label, nummer, naam, inhoud, parent_id
            FROM wro.wro_tekst_object
            WHERE instrument_idn = %s
            ORDER BY volgnummer
        """, (instrument_idn,))
        rows = cur.fetchall()

        if not rows:
            return 0

        # Stap A: Vind alle unieke parent_id's die niet als rij bestaan
        existing_ids = {r["identificatie"] for r in rows}
        virtual_parents: dict[str, dict] = {}
        chapter_counter = 0

        # Groepeer kinderen per parent_id voor naam-afleiding
        children_by_parent: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r["parent_id"]:
                children_by_parent[r["parent_id"]].append(r)

        # Bucket per rij — door de boom omhoog wandelen tot de rootrij die
        # zichzelf classificeert (Regels/Bijlage/Toelichting/Overig). Een rij
        # waarvan parent_id naar een niet-bestaande string wijst valt onder
        # virtual_parents en zit niet in deze map; die krijgen Lichaam.
        rows_by_id = {r["identificatie"]: r for r in rows}

        def _bucket_for_row(r: dict, depth: int = 0) -> str:
            if depth > 50:
                return "Lichaam"
            pid = r.get("parent_id")
            if pid is None or pid not in rows_by_id:
                return _classify_root(r.get("naam"), r.get("object_type"))
            return _bucket_for_row(rows_by_id[pid], depth + 1)

        row_bucket: dict[str, str] = {r["identificatie"]: _bucket_for_row(r) for r in rows}

        seen_nummers: dict[str, int] = {}  # track duplicaat-nummers
        for r in rows:
            pid = r["parent_id"]
            if pid and pid not in existing_ids and pid not in virtual_parents:
                chapter_counter += 1
                # Extraheer nummer + slug uit parent_id: "_1_Begrippen"
                match = re.search(r"_(\d+(?:\.\d+)*)_(.+)$", pid)
                if match:
                    nummer = match.group(1)
                    naam_slug = match.group(2)
                    naam = _derive_chapter_name(naam_slug, children_by_parent.get(pid, []))
                else:
                    nummer = str(chapter_counter)
                    naam = f"Hoofdstuk {chapter_counter}"

                # Disambigueer bij duplicaat-nummers (bv. twee keer "_3_...")
                if nummer in seen_nummers:
                    seen_nummers[nummer] += 1
                    suffix = chr(ord("a") + seen_nummers[nummer] - 1)  # 3a, 3b, ...
                    nummer = f"{nummer}{suffix}"
                else:
                    seen_nummers[nummer] = 1

                # Bucket voor virtuele parents: zelfde heuristiek op de slug
                # (meeste virtuele hoofdstukken zijn gewoon planregels →
                # Lichaam, maar een slug die met "Bijlage"/"Toelichting" begint
                # belandt in de juiste sectie).
                vp_bucket = _classify_root(naam, None)

                virtual_parents[pid] = {
                    "nummer": nummer,
                    "naam": naam,
                    "niveau": 1,
                    "volgnummer": 0,
                    "bucket": vp_bucket,
                }

        # Stap B: Bouw alle elementen op (virtuele parents + echte rijen)
        # Pass B1: bepaal de FINALE eId per chapter (niveau ≤ 1) met
        # disambiguatie. Twee "Hoofdstuk 1"-en in dezelfde bucket — bv.
        # "Bijlagen bij regels" + "Bijlagen bij toelichting" beide met een
        # ongenummerd `1` — krijgen daar `__2`, `__3` achteraan zodat de
        # UNIQUE-constraint op (regeling_expression, eid) niet sneuvelt.
        elements: list[dict] = []
        chapter_eid_by_orig: dict[str, str] = {}
        seen_chapter_eids: dict[str, int] = {}

        def _claim_chapter_eid(base: str) -> str:
            if base not in seen_chapter_eids:
                seen_chapter_eids[base] = 1
                return base
            seen_chapter_eids[base] += 1
            return f"{base}__{seen_chapter_eids[base]}"

        # Virtuele hoofdstukken
        for pid, vp in virtual_parents.items():
            container_eid = _BUCKET_EID[vp["bucket"]]
            base_eid = _make_eid(1, vp["nummer"], 0, container_eid)
            eid = _claim_chapter_eid(base_eid)
            chapter_eid_by_orig[pid] = eid
            elements.append({
                "eid": eid,
                "wid": f"gm{bronhouder}__{eid}",
                "element_type": "Hoofdstuk",
                "nummer": vp["nummer"],
                "opschrift": vp["naam"],
                "inhoud": None,
                "parent_eid": container_eid,
                "bucket": vp["bucket"],
                "sort_key": (int(re.match(r"(\d+)", vp["nummer"]).group(1)) if re.match(r"\d", vp["nummer"]) else 999, chapter_counter),
                "original_id": pid,
            })

        # Echte niveau-1 chapters die direct onder een (geskipte) root hangen
        for r in rows:
            if r["parent_id"] is None or r["niveau"] > 1:
                continue
            parent_row = rows_by_id.get(r["parent_id"])
            if parent_row is None or parent_row.get("parent_id") is not None:
                # Geen echte chapter-positie (parent is virtueel of geneste rij)
                continue
            bucket = row_bucket[r["identificatie"]]
            container_eid = _BUCKET_EID[bucket]
            base_eid = _make_eid(1, r["nummer"], r["volgnummer"], container_eid)
            eid = _claim_chapter_eid(base_eid)
            chapter_eid_by_orig[r["identificatie"]] = eid
            nr_parts = r["nummer"].split(".") if r["nummer"] else [str(r["volgnummer"])]
            sort_key = tuple(int(p) if p.isdigit() else 999 for p in nr_parts)
            elements.append({
                "eid": eid,
                "wid": f"gm{bronhouder}__{eid}",
                "element_type": "Hoofdstuk",
                "nummer": r["nummer"],
                "opschrift": r["naam"] or r["label"],
                "inhoud": r["inhoud"],
                "parent_eid": container_eid,
                "bucket": bucket,
                "sort_key": sort_key,
                "original_id": r["identificatie"],
                "original_parent_id": r["parent_id"],
            })

        # Pass B2: artikelen / leden / overige rijen
        for r in rows:
            if r["parent_id"] is None:
                continue  # root — vertegenwoordigd door bucket-container
            if r["identificatie"] in chapter_eid_by_orig:
                continue  # al verwerkt als chapter

            bucket = row_bucket[r["identificatie"]]
            container_eid = _BUCKET_EID[bucket]

            # Parent eId resoluties:
            # 1) parent is een chapter (virtueel of echt): pak de finale eId.
            # 2) parent is een geneste rij: deferred — insert-fase regelt het
            #    via original_parent_id-lookup.
            parent_eid: str | None = None
            if r["parent_id"] in chapter_eid_by_orig:
                parent_eid = chapter_eid_by_orig[r["parent_id"]]
            elif r["parent_id"] in existing_ids:
                parent_eid = "__deferred__"

            # eId-prefix: gebruik chapter-eId als die bekend is, anders fall
            # back op de container zodat geneste artikel-eIds niet anoniem
            # `art_X` worden (en zo conflicten geven over secties heen).
            eid_prefix = parent_eid if parent_eid and parent_eid != "__deferred__" else container_eid
            eid = _make_eid(r["niveau"], r["nummer"], r["volgnummer"], eid_prefix)
            # Best-effort uniekheid voor articles (bv. twee "1.1" leden onder
            # geneste deferred parents in dezelfde sectie). Hergebruik hetzelfde
            # disambiguatie-mechanisme, maar zonder de chapter-mapping te
            # vervuilen — articles zijn nooit parent van een ander item.
            eid = _claim_chapter_eid(eid)

            nr_parts = r["nummer"].split(".") if r["nummer"] else [str(r["volgnummer"])]
            sort_key = tuple(int(p) if p.isdigit() else 999 for p in nr_parts)
            el_type = _element_type_from_niveau(r["niveau"], r["object_type"])

            elements.append({
                "eid": eid,
                "wid": f"gm{bronhouder}__{eid}",
                "element_type": el_type,
                "nummer": r["nummer"],
                "opschrift": r["naam"] or r["label"],
                "inhoud": r["inhoud"],
                "parent_eid": parent_eid,
                "bucket": bucket,
                "sort_key": sort_key,
                "original_id": r["identificatie"],
                "original_parent_id": r["parent_id"],
            })

        # Stap C: Insert in conv.tekst_element
        # Eerst de bucket-containers (Lichaam/Bijlage/Toelichting), dan
        # hoofdstukken (parent = container), dan artikelen (parent = eid-lookup).
        eid_to_db_id: dict[str, int] = {}
        original_id_to_db_id: dict[str, int] = {}

        used_buckets = {e["bucket"] for e in elements}
        container_db: dict[str, int] = {}

        volgorde = 0
        for bucket in _BUCKETS:
            if bucket not in used_buckets:
                continue
            volgorde += 1
            container_eid = _BUCKET_EID[bucket]
            container_wid = f"gm{bronhouder}__{container_eid}"
            cur.execute("""
                INSERT INTO conv.tekst_element
                    (regeling_expression, eid, wid, element_type,
                     parent_id, nummer, opschrift, inhoud, volgorde)
                VALUES (%s, %s, %s, %s, NULL, NULL, NULL, NULL, %s)
                RETURNING id
            """, (regeling_expression, container_eid, container_wid, bucket, volgorde))
            db_id = cur.fetchone()["id"]
            container_db[bucket] = db_id
            eid_to_db_id[container_eid] = db_id

        chapters = [e for e in elements if e["element_type"] == "Hoofdstuk"]
        chapters.sort(key=lambda e: (_BUCKETS.index(e["bucket"]), e["sort_key"]))
        articles = [e for e in elements if e["element_type"] != "Hoofdstuk"]
        articles.sort(key=lambda e: (_BUCKETS.index(e["bucket"]), e["sort_key"]))

        for el in chapters:
            volgorde += 1
            # Hoofdstukken hangen direct onder hun bucket-container, tenzij
            # ze al een echte parent hebben (zeldzame geneste hoofdstukken).
            parent_db_id = container_db[el["bucket"]]
            if el.get("parent_eid") and el["parent_eid"] not in (None, "__deferred__"):
                parent_db_id = eid_to_db_id.get(el["parent_eid"], parent_db_id)
            cur.execute("""
                INSERT INTO conv.tekst_element
                    (regeling_expression, eid, wid, element_type,
                     parent_id, nummer, opschrift, inhoud, volgorde)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (regeling_expression, el["eid"], el["wid"],
                  el["element_type"], parent_db_id, el["nummer"],
                  el["opschrift"], el["inhoud"], volgorde))
            db_id = cur.fetchone()["id"]
            eid_to_db_id[el["eid"]] = db_id
            if "original_id" in el:
                original_id_to_db_id[el["original_id"]] = db_id

        for el in articles:
            volgorde += 1
            # Resolve parent
            parent_db_id: int | None = None
            if el.get("parent_eid") and el["parent_eid"] != "__deferred__":
                parent_db_id = eid_to_db_id.get(el["parent_eid"])
            if parent_db_id is None and el.get("original_parent_id"):
                parent_db_id = original_id_to_db_id.get(el["original_parent_id"])
                if parent_db_id is None:
                    # Zoek via eid van het virtuele parent
                    for ch in chapters:
                        if ch.get("original_id") == el["original_parent_id"]:
                            parent_db_id = eid_to_db_id.get(ch["eid"])
                            break
            # Laatste fallback: artikel direct onder de bucket-container.
            if parent_db_id is None:
                parent_db_id = container_db[el["bucket"]]

            cur.execute("""
                INSERT INTO conv.tekst_element
                    (regeling_expression, eid, wid, element_type,
                     parent_id, nummer, opschrift, inhoud, volgorde)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (regeling_expression, el["eid"], el["wid"],
                  el["element_type"], parent_db_id, el["nummer"],
                  el["opschrift"], el["inhoud"], volgorde))
            db_id = cur.fetchone()["id"]
            eid_to_db_id[el["eid"]] = db_id
            if "original_id" in el:
                original_id_to_db_id[el["original_id"]] = db_id

        return len(elements) + len(used_buckets)


# ── Stap 1.3: Locaties ──────────────────────────────────────────────

def convert_locaties(conn: psycopg.Connection, instrument_idn: str,
                     bronhouder: str) -> dict[str, list[str]]:
    """Converteer wro.planobject → conv.locatie + locatiegroep_lid.

    Returns dict van bestemmingshoofdgroep → lijst van locatie-IDs
    (voor gebiedsaanwijzing-koppeling).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT po.identificatie, po.object_type, po.naam,
                   po.bestemmingshoofdgroep, po.geometrie
            FROM wro.planobject po
            WHERE po.instrument_idn = %s
        """, (instrument_idn,))
        planobjecten = cur.fetchall()

        if not planobjecten:
            return {}

        # Per planobject een locatie
        groep_leden: dict[str, list[str]] = defaultdict(list)  # groep_key → [locatie_ids]
        count = 0

        for po in planobjecten:
            loc_id = _uid("locatie", bronhouder)
            noemer = po["naam"] or po["bestemmingshoofdgroep"] or po["object_type"]

            cur.execute("""
                INSERT INTO conv.locatie
                    (identificatie, locatie_type, noemer, geometrie, bron_planobject)
                VALUES (%s, 'Gebied', %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (loc_id, noemer, po["geometrie"], po["identificatie"]))
            count += 1

            # Groepeer op (object_type, bestemmingshoofdgroep)
            groep_key = f"{po['object_type']}|{po['bestemmingshoofdgroep'] or 'geen'}"
            groep_leden[groep_key].append(loc_id)

        # Per groep een locatiegroep aanmaken
        groep_mapping: dict[str, list[str]] = {}
        for groep_key, leden in groep_leden.items():
            if len(leden) < 2:
                # Geen groep nodig voor singletons
                groep_mapping[groep_key] = leden
                continue

            groep_id = _uid("locatiegroep", bronhouder)
            obj_type, bhg = groep_key.split("|", 1)
            noemer = f"{obj_type} - {bhg}" if bhg != "geen" else obj_type

            # Union-geometrie voor de groep. ST_MakeValid wikkelt invalide
            # IMRO-polygonen (zelf-doorsnijdend, dubbele ringen) zodat
            # ST_Union niet faalt met een GEOS-error op één rotte feature.
            # CollectionExtract(...,3) houdt alleen polygonen over: MakeValid
            # kan op extreem kapotte input een GeometryCollection met punten
            # of lijnen produceren, die niet in onze GEOMETRY(28992)-kolom
            # passen.
            cur.execute("""
                INSERT INTO conv.locatie
                    (identificatie, locatie_type, noemer, geometrie)
                SELECT %s, 'Gebiedengroep', %s,
                       ST_CollectionExtract(ST_Union(ST_MakeValid(geometrie)), 3)
                FROM conv.locatie
                WHERE identificatie = ANY(%s)
            """, (groep_id, noemer, leden))

            for lid_id in leden:
                cur.execute("""
                    INSERT INTO conv.locatiegroep_lid
                        (groep_identificatie, lid_identificatie)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (groep_id, lid_id))

            groep_mapping[groep_key] = [groep_id]  # de groep-locatie als representant

        console.print(f"    {count} locaties, {sum(1 for v in groep_leden.values() if len(v) >= 2)} groepen")
        return groep_mapping


# ── Stap 1.4: Gebiedsaanwijzingen ────────────────────────────────────

def convert_gebiedsaanwijzingen(conn: psycopg.Connection,
                                 instrument_idn: str,
                                 bronhouder: str,
                                 groep_mapping: dict[str, list[str]]) -> int:
    """Maak conv.gebiedsaanwijzing aan op basis van bestemmingshoofdgroep."""
    count = 0
    with conn.cursor() as cur:
        # Unieke (object_type, bestemmingshoofdgroep) combinaties
        cur.execute("""
            SELECT DISTINCT po.object_type, po.bestemmingshoofdgroep, po.naam
            FROM wro.planobject po
            WHERE po.instrument_idn = %s
              AND po.bestemmingshoofdgroep IS NOT NULL
              AND po.object_type IN ('Enkelbestemming', 'Dubbelbestemming')
        """, (instrument_idn,))

        for row in cur.fetchall():
            bhg = row["bestemmingshoofdgroep"].lower() if row["bestemmingshoofdgroep"] else None
            if not bhg or bhg not in BESTEMMING_NAAR_OW:
                continue

            ga_type, ga_groep = BESTEMMING_NAAR_OW[bhg]
            ga_naam = row["naam"] or row["bestemmingshoofdgroep"]

            # Zoek de locatie(groep) voor deze combinatie
            groep_key = f"{row['object_type']}|{row['bestemmingshoofdgroep'] or 'geen'}"
            loc_ids = groep_mapping.get(groep_key, [])
            if not loc_ids:
                continue

            loc_id = loc_ids[0]  # de groep-locatie of singleton

            ga_id = _uid("gebiedsaanwijzing", bronhouder)
            cur.execute("""
                INSERT INTO conv.gebiedsaanwijzing
                    (identificatie, type, naam, groep, locatie_id, bron)
                VALUES (%s, %s, %s, %s, %s, 'mechanisch')
                ON CONFLICT DO NOTHING
            """, (ga_id, ga_type, ga_naam, ga_groep, loc_id))
            count += 1

    return count


# ── Stap 1.5: Regelingsgebied ────────────────────────────────────────

def convert_regelingsgebied(conn: psycopg.Connection,
                             instrument_idn: str,
                             bronhouder: str) -> str | None:
    """Maak conv.locatie aan voor het regelingsgebied (plangebied)."""
    with conn.cursor() as cur:
        loc_id = _uid("locatie", bronhouder)
        cur.execute("""
            INSERT INTO conv.locatie
                (identificatie, locatie_type, noemer, geometrie, bron_planobject)
            SELECT %s, 'Gebied', 'Regelingsgebied ' || ri.naam,
                   ri.geometrie, ri.idn
            FROM wro.ruimtelijk_instrument ri
            WHERE ri.idn = %s
            RETURNING identificatie
        """, (loc_id, instrument_idn))
        row = cur.fetchone()
        return row["identificatie"] if row else None


# ── Stap 1.6: Metadata ──────────────────────────────────────────────

def log_conversie(conn: psycopg.Connection, instrument_idn: str,
                  regeling_expression: str) -> None:
    """Schrijf conv.conversie_meta."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO conv.conversie_meta
                (instrument_idn, regeling_expression, stap, bron)
            VALUES (%s, %s, 1, 'mechanisch')
        """, (instrument_idn, regeling_expression))


# ── Orchestrator ─────────────────────────────────────────────────────

def convert_bestemmingsplan(instrument_idn: str) -> dict:
    """Voer stap 1 uit voor één bestemmingsplan.

    Returns dict met statistieken.
    """
    conn = get_conn()
    try:
        # Haal bronhouder op
        with conn.cursor() as cur:
            cur.execute("""
                SELECT bronhouder, naam FROM wro.ruimtelijk_instrument
                WHERE idn = %s
            """, (instrument_idn,))
            ri = cur.fetchone()
            if not ri:
                raise ValueError(f"Instrument niet gevonden: {instrument_idn}")

        bronhouder = ri["bronhouder"]
        console.rule(f"[bold]Conversie: {ri['naam']}[/bold] ({instrument_idn})")

        # 1.1 Regeling
        console.print("  [dim]1.1 Regeling aanmaken...[/dim]")
        regeling_expr = convert_regeling(conn, instrument_idn)
        console.print(f"    {regeling_expr}")

        # 1.2 Tekst
        console.print("  [dim]1.2 Tekst overnemen...[/dim]")
        n_tekst = convert_tekst(conn, instrument_idn, regeling_expr)
        console.print(f"    {n_tekst} tekst-elementen")

        # 1.3 Locaties
        console.print("  [dim]1.3 Locaties + groepering...[/dim]")
        groep_mapping = convert_locaties(conn, instrument_idn, bronhouder)

        # 1.4 Gebiedsaanwijzingen
        console.print("  [dim]1.4 Gebiedsaanwijzingen...[/dim]")
        n_ga = convert_gebiedsaanwijzingen(conn, instrument_idn, bronhouder, groep_mapping)
        console.print(f"    {n_ga} gebiedsaanwijzingen")

        # 1.5 Regelingsgebied
        console.print("  [dim]1.5 Regelingsgebied...[/dim]")
        rg_id = convert_regelingsgebied(conn, instrument_idn, bronhouder)

        # 1.6 Metadata
        log_conversie(conn, instrument_idn, regeling_expr)

        conn.commit()

        stats = {
            "instrument_idn": instrument_idn,
            "regeling_expression": regeling_expr,
            "tekst_elementen": n_tekst,
            "locaties": sum(len(v) for v in groep_mapping.values()),
            "gebiedsaanwijzingen": n_ga,
            "regelingsgebied": rg_id,
        }
        console.print(f"  [green]Stap 1 voltooid: {n_tekst} teksten, "
                      f"{stats['locaties']} locaties, {n_ga} gebiedsaanwijzingen[/green]")
        return stats

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def convert_gemeente(bronhouder_code: str) -> list[dict]:
    """Converteer alle vigerende bestemmingsplannen van een gemeente."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.idn, ri.naam
                FROM wro.ruimtelijk_instrument ri
                JOIN wro.wro_tekst_object wt ON wt.instrument_idn = ri.idn
                WHERE ri.bronhouder = %s
                  AND ri.planstatus = 'vastgesteld'
                GROUP BY ri.idn, ri.naam
                HAVING count(wt.identificatie) > 0
                ORDER BY ri.naam
            """, (bronhouder_code,))
            plannen = cur.fetchall()
    finally:
        conn.close()

    console.print(f"[bold]{len(plannen)} bestemmingsplannen met tekst voor gemeente {bronhouder_code}[/bold]")
    results = []
    for plan in plannen:
        try:
            stats = convert_bestemmingsplan(plan["idn"])
            results.append(stats)
        except Exception as e:
            console.print(f"  [red]Fout bij {plan['idn']}: {e}[/red]")
            results.append({"instrument_idn": plan["idn"], "error": str(e)})
    return results


def clear_gemeente(bronhouder_code: str) -> int:
    """Wis alle conversie-data voor een gemeente (voor re-run).

    Verwijdert in FK-volgorde: junctions → objecten → locaties → regeling.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Vind regelingen van deze gemeente
            cur.execute("SELECT frbr_expression FROM conv.regeling WHERE bronhouder = %s",
                        (bronhouder_code,))
            expressions = [r["frbr_expression"] for r in cur.fetchall()]
            if not expressions:
                return 0

            # Verwijder in FK-volgorde: junctions → objecten → locaties → regeling
            bh_pattern = f"%gm{bronhouder_code}%"
            cur.execute("DELETE FROM conv.juridische_regel_norm WHERE juridische_regel_id LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.juridische_regel_gebiedsaanwijzing WHERE juridische_regel_id LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.activiteit_locatieaanduiding WHERE activiteit_id LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.normwaarde WHERE norm_id LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.norm WHERE identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.juridische_regel WHERE identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.activiteit WHERE identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.gebiedsaanwijzing WHERE identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.locatiegroep_lid WHERE groep_identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.locatie WHERE identificatie LIKE %s", (bh_pattern,))
            cur.execute("DELETE FROM conv.conversie_meta WHERE instrument_idn LIKE %s",
                        (f"NL.IMRO.{bronhouder_code}.%",))
            for expr in expressions:
                cur.execute("DELETE FROM conv.regeling WHERE frbr_expression = %s", (expr,))
        conn.commit()
        return len(expressions)
    finally:
        conn.close()
