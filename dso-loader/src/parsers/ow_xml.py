"""Parse OW-bestanden/*.xml into CIM-OW database tables."""

from lxml import etree

# IMOW namespaces (from the XML)
NS = {
    "ow-dc": "http://www.geostandaarden.nl/imow/bestanden/deelbestand",
    "sl": "http://www.geostandaarden.nl/bestanden-ow/standlevering-generiek",
    "rol": "http://www.geostandaarden.nl/imow/regelsoplocatie",
    "regels": "http://www.geostandaarden.nl/imow/regels",
    "l": "http://www.geostandaarden.nl/imow/locatie",
    "ga": "http://www.geostandaarden.nl/imow/gebiedsaanwijzing",
    "rg": "http://www.geostandaarden.nl/imow/regelingsgebied",
    "ow": "http://www.geostandaarden.nl/imow/owobject",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _text(elem, xpath: str) -> str | None:
    """Extract text from XPath relative to elem."""
    found = elem.find(xpath, NS)
    if found is not None and found.text:
        return found.text.strip()
    return None


def _href(elem, xpath: str) -> str | None:
    """Extract xlink:href attribute from XPath."""
    found = elem.find(xpath, NS)
    if found is not None:
        return found.get(f"{{{NS['xlink']}}}href")
    return None


def _parse_ow_xml(xml_bytes: bytes):
    """Parse OW XML with recovery mode for malformed namespace declarations."""
    parser = etree.XMLParser(recover=True)
    return etree.fromstring(xml_bytes, parser=parser)


def parse_activiteiten(xml_bytes: bytes) -> list[dict]:
    """Parse activiteiten.xml → list of activiteit dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for act in root.findall(".//rol:Activiteit", NS):
        identificatie = _text(act, "rol:identificatie")
        naam = _text(act, "rol:naam")
        groep = _text(act, "rol:groep")
        bovenliggende = _href(act, "rol:bovenliggendeActiviteit/rol:ActiviteitRef")

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "naam": naam or "",
                "groep": groep,
                "bovenliggende": bovenliggende,
            })

    return results


def parse_juridische_regels(xml_bytes: bytes) -> list[dict]:
    """Parse regelsvooriedereen.xml → list of juridische_regel dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for tag_name, regel_type in [
        ("regels:RegelVoorIedereen", "RegelVoorIedereen"),
        ("regels:Instructieregel", "Instructieregel"),
        ("regels:Omgevingswaarderegel", "Omgevingswaarderegel"),
    ]:
        for regel in root.findall(f".//{tag_name}", NS):
            identificatie = _text(regel, "regels:identificatie")
            idealisatie_raw = _text(regel, "regels:idealisatie") or ""

            # Normalize idealisatie URI to just the code
            idealisatie = idealisatie_raw.split("/")[-1] if "/" in idealisatie_raw else idealisatie_raw
            # Map to our lookup: Exact -> exact, Indicatief -> indicatief
            idealisatie = idealisatie.lower() if idealisatie else "exact"

            # Get regeltekst reference
            regeltekst_wid = _href(regel, "regels:artikelOfLid/regels:RegeltekstRef")

            # Get activiteit locatie aanduidingen
            # Structure: regels:activiteitaanduiding > rol:ActiviteitRef + regels:ActiviteitLocatieaanduiding
            alas = []
            for aa in regel.findall("regels:activiteitaanduiding", NS):
                act_ref = _href(aa, "rol:ActiviteitRef")
                for ala in aa.findall("regels:ActiviteitLocatieaanduiding", NS):
                    loc_ref = _href(ala, "regels:locatieaanduiding/l:LocatieRef")
                    kwalificatie_raw = _text(ala, "regels:activiteitregelkwalificatie") or ""
                    kwalificatie = kwalificatie_raw.split("/")[-1] if "/" in kwalificatie_raw else kwalificatie_raw

                    if act_ref and loc_ref:
                        alas.append({
                            "activiteit_id": act_ref,
                            "locatie_id": loc_ref,
                            "kwalificatie": kwalificatie,
                        })

            # Get gebiedsaanwijzing references
            ga_refs = []
            for ga_ref in regel.findall("regels:gebiedsaanwijzing/ga:GebiedsaanwijzingRef", NS):
                href = ga_ref.get(f"{{{NS['xlink']}}}href")
                if href:
                    ga_refs.append(href)

            # Get omgevingsnorm references
            norm_refs = []
            for norm_ref in regel.findall("regels:omgevingsnormRef/rol:OmgevingsnormRef", NS):
                href = norm_ref.get(f"{{{NS['xlink']}}}href")
                if href:
                    norm_refs.append(href)

            if identificatie and regeltekst_wid:
                results.append({
                    "identificatie": identificatie,
                    "regel_type": regel_type,
                    "idealisatie": idealisatie,
                    "regeltekst_wid": regeltekst_wid,
                    "activiteit_locatie_aanduidingen": alas,
                    "gebiedsaanwijzing_refs": ga_refs,
                    "norm_refs": norm_refs,
                })

    return results


def parse_gebieden(xml_bytes: bytes) -> list[dict]:
    """Parse gebieden.xml → list of locatie dicts with GeometrieRef."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for gebied in root.findall(".//l:Gebied", NS):
        identificatie = _text(gebied, "l:identificatie")
        noemer = _text(gebied, "l:noemer")
        geom_ref = _href(gebied, "l:geometrie/l:GeometrieRef")

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "locatie_type": "Gebied",
                "noemer": noemer,
                "geometrie_ref": geom_ref,
            })

    for punt in root.findall(".//l:Punt", NS):
        identificatie = _text(punt, "l:identificatie")
        noemer = _text(punt, "l:noemer")
        geom_ref = _href(punt, "l:geometrie/l:GeometrieRef")

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "locatie_type": "Punt",
                "noemer": noemer,
                "geometrie_ref": geom_ref,
            })

    return results


def parse_gebiedengroepen(xml_bytes: bytes) -> list[dict]:
    """Parse gebiedengroepen.xml → list of locatiegroep dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for groep in root.findall(".//l:Gebiedengroep", NS):
        identificatie = _text(groep, "l:identificatie")
        noemer = _text(groep, "l:noemer")

        leden = []
        for lid_ref in groep.findall("l:groepselement/l:GebiedRef", NS):
            href = lid_ref.get(f"{{{NS['xlink']}}}href")
            if href:
                leden.append(href)

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "locatie_type": "Gebiedengroep",
                "noemer": noemer,
                "leden": leden,
            })

    return results


def parse_gebiedsaanwijzingen(xml_bytes: bytes) -> list[dict]:
    """Parse gebiedsaanwijzingen.xml → list of gebiedsaanwijzing dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for ga_elem in root.findall(".//ga:Gebiedsaanwijzing", NS):
        identificatie = _text(ga_elem, "ga:identificatie")
        type_val = _text(ga_elem, "ga:type") or ""
        naam = _text(ga_elem, "ga:naam") or ""
        groep = _text(ga_elem, "ga:groep")
        locatie_ref = _href(ga_elem, "ga:locatieaanduiding/l:LocatieRef")

        if identificatie:
            # Normalize type URI to just the code
            type_code = type_val.split("/")[-1] if "/" in type_val else type_val

            results.append({
                "identificatie": identificatie,
                "type": type_code,
                "naam": naam,
                "groep": groep,
                "locatie_id": locatie_ref,
            })

    return results


def parse_ponsen(xml_bytes: bytes) -> list[dict]:
    """Parse ponsen.xml → list of pons dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    ns_p = "http://www.geostandaarden.nl/imow/pons"
    for pons in root.findall(f".//{{{ns_p}}}Pons"):
        ident_el = pons.find(f"{{{ns_p}}}identificatie")
        identificatie = ident_el.text.strip() if ident_el is not None and ident_el.text else None

        loc_ref = pons.find(f"{{{ns_p}}}locatieaanduiding/{{http://www.geostandaarden.nl/imow/locatie}}LocatieRef")
        locatie_id = loc_ref.get(f"{{{NS['xlink']}}}href") if loc_ref is not None else None

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "locatie_id": locatie_id,
            })

    return results


def parse_omgevingsnormen(xml_bytes: bytes) -> list[dict]:
    """Parse omgevingsnormen.xml → list of norm dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    # Omgevingsnorm can be in rol: or regels: namespace
    ns_rol = NS["rol"]
    ns_regels = NS["regels"]
    all_normen = root.findall(f".//{{{ns_rol}}}Omgevingsnorm") + root.findall(f".//{{{ns_regels}}}Omgevingsnorm")
    for norm in all_normen:
        # Try both namespaces for each field
        identificatie = None
        for ns in [ns_rol, ns_regels]:
            id_el = norm.find(f"{{{ns}}}identificatie")
            if id_el is not None and id_el.text:
                identificatie = id_el.text.strip()
                break

        naam = ""
        for ns in [ns_rol, ns_regels]:
            naam_el = norm.find(f"{{{ns}}}naam")
            if naam_el is not None and naam_el.text:
                naam = naam_el.text.strip()
                break

        type_val = ""
        for ns in [ns_rol, ns_regels]:
            type_el = norm.find(f"{{{ns}}}type")
            if type_el is not None and type_el.text:
                type_val = type_el.text.strip()
                break
        type_code = type_val.split("/")[-1] if "/" in type_val else type_val

        groep = None
        for ns in [ns_rol, ns_regels]:
            groep_el = norm.find(f"{{{ns}}}groep")
            if groep_el is not None and groep_el.text:
                groep = groep_el.text.strip()
                break

        # Parse normwaarden (nested within the norm)
        normwaarden = []
        for ns in [ns_rol, ns_regels]:
            for nw in norm.findall(f"{{{ns}}}normwaarde/{{{ns}}}Normwaarde"):
                nw_id = None
                for ns2 in [ns_rol, ns_regels]:
                    el = nw.find(f"{{{ns2}}}identificatie")
                    if el is not None and el.text:
                        nw_id = el.text.strip()
                        break

                kwant = None
                for ns2 in [ns_rol, ns_regels]:
                    el = nw.find(f"{{{ns2}}}kwantitatieveWaarde")
                    if el is not None and el.text:
                        kwant = el.text.strip()
                        break

                kwali = None
                for ns2 in [ns_rol, ns_regels]:
                    el = nw.find(f"{{{ns2}}}kwalitatieveWaarde")
                    if el is not None and el.text:
                        kwali = el.text.strip()
                        break

                loc_ref = _href(nw, f"{{{ns}}}locatieaanduiding/l:LocatieRef")

                if nw_id:
                    normwaarden.append({
                        "identificatie": nw_id,
                        "kwantitatieve_waarde": kwant,
                        "kwalitatieve_waarde": kwali,
                        "locatie_id": loc_ref,
                    })

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "naam": naam,
                "type_norm": type_code,
                "groep": groep,
                "norm_type": "Omgevingsnorm",
                "normwaarden": normwaarden,
            })

    return results


def parse_regelteksten(xml_bytes: bytes) -> list[dict]:
    """Parse regelteksten.xml → list of regeltekst location mappings."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for rt in root.findall(".//regels:Regeltekst", NS):
        identificatie = _text(rt, "regels:identificatie")

        if identificatie:
            results.append({
                "identificatie": identificatie,
            })

    return results


VT_NS = "http://www.geostandaarden.nl/imow/vrijetekst"


def parse_tekstdelen(xml_bytes: bytes) -> list[dict]:
    """Parse tekstdelen.xml → list of tekstdeel dicts for vrijetekst-instrumenten."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for td in root.findall(f".//{{{VT_NS}}}Tekstdeel"):
        identificatie = td.findtext(f"{{{VT_NS}}}identificatie")
        if identificatie:
            identificatie = identificatie.strip()

        idealisatie_raw = td.findtext(f"{{{VT_NS}}}idealisatie") or ""
        idealisatie = idealisatie_raw.split("/")[-1].lower() if "/" in idealisatie_raw else idealisatie_raw.lower()
        if not idealisatie:
            idealisatie = "exact"

        divisie_ref = None
        for ref_tag in ("DivisietekstRef", "DivisieRef"):
            ref_el = td.find(f"{{{VT_NS}}}divisieaanduiding/{{{VT_NS}}}{ref_tag}")
            if ref_el is not None:
                divisie_ref = ref_el.get(f"{{{NS['xlink']}}}href")
                break

        locatie_ref = _href(td, f"{{{VT_NS}}}locatieaanduiding/l:LocatieRef")

        thema = td.findtext(f"{{{VT_NS}}}thema")
        if thema:
            thema = thema.strip()

        hoofdlijn_refs = []
        for hl_ref in td.findall(f"{{{VT_NS}}}hoofdlijnaanduiding/{{{VT_NS}}}HoofdlijnRef"):
            href = hl_ref.get(f"{{{NS['xlink']}}}href")
            if href:
                hoofdlijn_refs.append(href)

        if identificatie and divisie_ref:
            results.append({
                "identificatie": identificatie,
                "divisie_wid": divisie_ref,
                "idealisatie": idealisatie,
                "locatie_id": locatie_ref,
                "thema": thema,
                "hoofdlijn_refs": hoofdlijn_refs,
            })

    return results


def parse_hoofdlijnen(xml_bytes: bytes) -> list[dict]:
    """Parse hoofdlijnen.xml → list of hoofdlijn dicts."""
    root = _parse_ow_xml(xml_bytes)
    results = []

    for hl in root.findall(f".//{{{VT_NS}}}Hoofdlijn"):
        identificatie = hl.findtext(f"{{{VT_NS}}}identificatie")
        if identificatie:
            identificatie = identificatie.strip()
        soort = hl.findtext(f"{{{VT_NS}}}soort") or ""
        naam = hl.findtext(f"{{{VT_NS}}}naam") or ""

        if identificatie:
            results.append({
                "identificatie": identificatie,
                "soort": soort.strip(),
                "naam": naam.strip(),
            })

    return results
