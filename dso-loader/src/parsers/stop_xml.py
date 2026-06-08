"""Parse STOP Regeling/Tekst.xml into tekst_element adjacency list."""

from lxml import etree

TEKST_NS = "https://standaarden.overheid.nl/stop/imop/tekst/"

# Elements that form the text tree
STRUCTURE_TYPES = {
    "RegelingCompact", "RegelingKlassiek", "RegelingVrijetekst", "RegelingTijdelijkdeel",
    "Lichaam", "Boek", "Deel", "Hoofdstuk", "Afdeling", "Titel",
    "Paragraaf", "Subparagraaf", "Subsubparagraaf",
    "Artikel", "Lid", "Divisie", "Divisietekst",
    "Bijlage", "Toelichting", "AlgemeneToelichting", "ArtikelgewijzeToelichting",
    "RegelingOpschrift",
}


def parse_tekst_xml(xml_bytes: bytes, regeling_expression: str) -> list[dict]:
    """Parse STOP Tekst.xml and return flat list of tekst_element dicts.

    Each dict has: eid, wid, element_type, parent_eid, nummer, opschrift, inhoud, volgorde
    """
    root = etree.fromstring(xml_bytes)
    elements = []
    volgorde_counter = [0]

    def _walk(node, parent_eid: str | None):
        # Sla XML-commentaar/processing-instructions over: hun .tag is geen string
        # maar een lxml-cyfunction, wat etree.QName laat crashen.
        if not isinstance(node.tag, str):
            return
        tag = etree.QName(node.tag).localname if node.tag else ""

        if tag not in STRUCTURE_TYPES:
            return

        eid = node.get("eId", "")
        wid = node.get("wId", eid)

        if not eid and tag in ("RegelingCompact", "RegelingKlassiek",
                               "RegelingVrijetekst", "RegelingTijdelijkdeel"):
            eid = "root"
            wid = "root"

        # Extract Kop children
        nummer = None
        opschrift = None
        kop = node.find(f"{{{TEKST_NS}}}Kop")
        if kop is not None:
            num_el = kop.find(f"{{{TEKST_NS}}}Nummer")
            if num_el is not None and num_el.text:
                nummer = num_el.text
            ops_el = kop.find(f"{{{TEKST_NS}}}Opschrift")
            if ops_el is not None and ops_el.text:
                opschrift = ops_el.text

        # Extract text content for leaf elements (Lid, Artikel without sub-elements, Divisietekst)
        inhoud = None
        if tag in ("Lid", "Divisietekst"):
            inhoud_el = node.find(f"{{{TEKST_NS}}}Inhoud")
            if inhoud_el is not None:
                inhoud = etree.tostring(inhoud_el, encoding="unicode", method="text")[:10000]
            elif tag == "Lid":
                # Some Leden have Al directly
                al_el = node.find(f".//{{{TEKST_NS}}}Al")
                if al_el is not None and al_el.text:
                    inhoud = al_el.text[:10000]

        volgorde_counter[0] += 1

        elements.append({
            "regeling_expression": regeling_expression,
            "eid": eid,
            "wid": wid,
            "element_type": tag,
            "parent_eid": parent_eid,
            "nummer": nummer,
            "opschrift": opschrift,
            "inhoud": inhoud,
            "volgorde": volgorde_counter[0],
        })

        # Recurse into children
        for child in node:
            _walk(child, eid)

    _walk(root, None)
    return elements
