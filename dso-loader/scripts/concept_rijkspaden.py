"""Concept: regelset uitbreiden voor de rijkspaden (AMvB). Simuleert, schrijft niets in de DB.

Hoort bij curatie/rijkspaden-concept-2026-09.xlsx. Uitkomst naar logs/concept_rijkspaden.json.

Twee soorten voorstel:
  CONTAINERS  structuurkopjes van de AMvB's die nu als label gekozen worden,
              zodat het onderwerp eronder niet bekeken wordt.
  REGELS      nieuwe (patroon, categorie, subcategorie), vóór de bestaande
              gezet omdat ze specifieker zijn.

Meet: winst op de AMvB's, en regressie op álle andere artikelen.
"""
import importlib.util, json, os, re, sys, collections
sys.path.insert(0, "c:/GIT/OCD/dso-loader")
spec = importlib.util.spec_from_file_location("bi", "c:/GIT/OCD/dso-loader/scripts/bouw_indeling.py")
bi = importlib.util.module_from_spec(spec); spec.loader.exec_module(bi)
from psycopg.rows import tuple_row
from src.db import get_conn

# ---------------------------------------------------------------------------
# id, ruw kopje, toelichting
CONTAINERS = [
    ("C01", "Milieubelastende activiteiten en lozingsactiviteiten: inhoudelijke regels", "Bal hfd 4 — boekdeel; het onderwerp is de activiteit eronder"),
    ("C02", "Milieubelastende activiteiten en lozingsactiviteiten: richtingaanwijzer", "Bal hfd 3 — wegwijzer per bedrijfstype"),
    ("C03", "Milieubelastende activiteiten: modules", "Bal — boekdeel"),
    ("C04", "Overige modules", "Bal — boekdeel"),
    ("C05", "Omgevingsplannen", "Bkl — het instrument waaraan de instructie gericht is, niet het onderwerp"),
    ("C06", "Omgevingsverordeningen", "Bkl — idem"),
    ("C07", "Waterschapsverordeningen", "Bkl — idem"),
    ("C08", "Instructieregels met het oog op een evenwichtige toedeling van functies aan locaties", "Bkl — oogmerk, geen onderwerp"),
    ("C09", "Beschermen van de gezondheid en van het milieu", "Bkl — oogmerk-kopje boven geluid/geur/lucht"),
    ("C10", "Waarborgen van de veiligheid", "Bkl — oogmerk-kopje"),
    ("C11", "Specifieke taken", "Bkl hfd 3 — boekdeel"),
    ("C12", "Programma’s", "Bkl hfd 4 — het instrument, niet het onderwerp"),
    ("C13", "Omgevingswaarden", "Bkl hfd 2 — het instrument"),
    ("C14", "Omgevingswaarden beschermen van de gezondheid en van het milieu", "Bkl — oogmerk"),
    ("C15", "Omgevingswaarden waarborgen van de veiligheid", "Bkl — oogmerk"),
    ("C16", "Bestaande bouw", "Bbl hfd 3 — boekdeel"),
    ("C17", "Algemene bepalingen voor bouwwerken", "Bbl hfd 2 — boekdeel"),
    ("C18", "Veiligheid", "Bbl — afdelingskopje boven brand, vluchtroutes, afscheidingen (alleen kaal 'veiligheid')"),
]

# id, patroon, categorie, subcategorie, nieuw?, toelichting
REGELS = [
    # -- veiligheid ---------------------------------------------------------
    ("R01", r"seveso|vuurwerk|pyrotechn", "veiligheid", "externe veiligheid", "", "Bal seveso-inrichting, vuurwerkopslag"),
    ("R02", r"\bbrand\b|rookmelder|blus", "veiligheid", "brandveiligheid", "", "Bbl brand- en rookeisen; \\b voorkomt 'brandbare vloeistoffen'; 'rook' los ving 'hinder door geur en rook'"),
    ("R03", r"defensie|militair", "veiligheid", "defensie", "nieuw", "Bal defensie, militaire oefeningen, Ob/Bkl militaire terreinen"),
    # -- bouwen ---------------------------------------------------------------
    ("R04", r"kwaliteitsborg|ce-markering|kwaliteitsverklaring", "bouwen", "kwaliteitsborging bouw", "nieuw", "Bbl/Ob stelsel kwaliteitsborging"),
    ("R05", r"hoogteverschil|afscheiding aan de rand|daglicht|toiletruimte|^verlichting$|^bruikbaarheid$|verwarmingssyst|gebouwautomatisering|bouwwerkinstallatie|tunnelveiligheid|hulpverleningsdienst",
     "bouwen", "bouwtechnische eisen", "nieuw", "Bbl technische bouwvoorschriften; 'verlichting'/'bruikbaarheid'/'afscheiding' alleen als hele Bbl-kop"),
    ("R06", r"energielabel|^duurzaamheid$", "energie", "energiebesparing", "", "Bbl duurzaamheid en energielabel"),
    ("R07", r"verbouw|gebruiksfunctie|gebruiksmelding", "bouwen", "bouwen en verbouwen", "", "Bbl verbouw, wijziging gebruiksfunctie"),
    # -- energie --------------------------------------------------------------
    ("R08", r"bodemenergie", "energie", "bodemenergie", "nieuw", "Bal open/gesloten bodemenergiesysteem — moet vóór de bodemregels"),
    ("R09", r"verduurzaming van het energie|energie-effici", "energie", "energiebesparing", "", "Bal modules energie"),
    # -- milieu: activiteiten ---------------------------------------------------
    ("R10", r"metalen|metaalrecycl", "milieu", "metaalbewerking", "", "bestaande regel ving 'metaal' maar niet 'metalen'"),
    ("R11", r"afvalbeheer|recycl|demontage|shredder|milieustraat|afvalstoffen", "milieu", "afval", "", "\\bafval\\b ving 'afvalbeheer' niet"),
    ("R12", r"oplosmiddel|coaten|lijmen|polyesterhars|rubber|titaandioxide|basischemie|cokes|raffinaderij|clausinstallatie|grafische processen|asfaltcentrale",
     "milieu", "chemie en procesindustrie", "nieuw", "Bal industriële activiteiten zonder eigen plek in de lijst"),
    ("R13", r"chemisch reinigen|wasserij|textiel", "milieu", "textielreiniging", "nieuw", "Bal chemische wasserij"),
    ("R14", r"mechanisch bewerken|bewerken van steen", "milieu", "mechanische bewerking", "nieuw", "Bal steen en diverse materialen"),
    ("R15", r"laboratori|^onderzoeken$|genetisch gemodificeerd|ziekenhuis|crematori", "milieu", "laboratoria en instellingen", "nieuw", "Bal"),
    ("R16", r"benzineterminal|tankstation", "milieu", "tanken en brandstoffen", "", "bestaande subcategorie"),
    ("R17", r"graffiti|wasstraat|wasplaats|reinigen van voertuigen", "milieu", "wassen van voertuigen", "", "bestaande subcategorie"),
    ("R18", r"koeltoren|zeer zorgwekkende|ongewone voorvallen|prtr", "milieu", "milieubelastende activiteiten overig", "", "restcategorie"),
    ("R19", r"datacentr", "economie", "datacenters", "", "bestaande subcategorie"),
    ("R39", r"zendmast", "infrastructuur", "telecommunicatie", "nieuw", "Bal zendmasten"),
    # -- bodem en water ---------------------------------------------------------
    ("R20", r"^bodembeschermende voorzieningen$", "bodem", "bodembeheer", "", "Bal-module; alleen als hele kop, anders pakt hij lozingsartikelen over hemelwater"),
    ("R21", r"baggerspecie|toepassen van grond|zuiveringsslib|ontgronding", "bodem", "graven en grondverzet", "", "Bal grond, bagger, slib"),
    ("R22", r"mijnbouw", "bodem", "mijnbouw", "nieuw", "Bal/Bkl mijnbouwlocaties"),
    ("R23", r"^zuiveringtechnisch werk$", "water", "afvalwater", "", "rwzi; alleen als hele kop, anders pakt hij bestaande lozingsartikelen"),
    ("R24", r"waterkwaliteit|waterveiligheid|waterprogramma|watersystemen|grote rivieren|\bkust\b|rijksvaarweg|waterkering|krw|grondwaterlicham|oppervlaktewaterlicham",
     "water", "watersysteem en waterkeringen", "", "Bkl waterkwaliteit/-veiligheid; '\\bwater\\b' ving samenstellingen niet"),
    ("R25", r"zwemmen|zwembad|zwemvijver|badwater|zwemlocatie", "recreatie", "zwemwater en badinrichtingen", "nieuw", "Bal/Bkl zwemmen en baden"),
    ("R26", r"noordzee|stortingsactiviteiten op zee|walvis", "water", "Noordzee", "nieuw", "Bal activiteiten in de Noordzee"),
    # -- landbouw en natuur -----------------------------------------------------
    ("R27", r"dierenverblijf|mestvergist|mestbehandel", "landbouw", "veehouderij", "", "Bal dierenverblijven en mest"),
    ("R28", r"^agrarische sector$|agrarisch loonwerk|substraatteelt|bloembollen|fruit|assimilatiebelichting|gietwater|zode van gras|landbouwmechanisatie|glastuinbouw",
     "landbouw", "teelt en tuinbouw", "", "Bal agrarische sector"),
    ("R29", r"\bjacht\b|uitoefening van de jacht|habitats en soorten|natura 2000|passende beoordeling|stikstof",
     "natuur", "natuur en bomen", "", "Bal/Bkl natuur"),
    # -- mobiliteit -------------------------------------------------------------
    ("R30", r"luchthaven|luchtvaart|helikopter|vliegtuig", "mobiliteit", "luchtvaart", "nieuw", "Luchthavenbesluit, traumahelikopter"),
    ("R31", r"jachthaven|pleziervaart|beroepsvaart|scheepswerf|ligplaats|laden en lossen van vaartuigen", "mobiliteit", "scheepvaart en havens", "nieuw", "Bal; niet 'vaartuig' los, dat pakt stalling en lozen vanaf vaartuigen"),
    ("R32", r"rijkswegen", "infrastructuur", "wegen en spoorwegen", "", "'\\bwegen\\b' ving 'rijkswegen' niet"),
    ("R33", r"aardgas", "infrastructuur", "kabels en leidingen", "", "Bal regelen en meten van aardgas"),
    # -- procedureel en bestuurlijk ----------------------------------------------
    ("R34", r"handhaving|sanctie|strafbaarstelling|bestuurlijke boete", "procedures", "handhaving en toezicht", "nieuw", "Ob hfd 13/18"),
    ("R35", r"financi|kostenverhaal|\bheffing|geldelijke|\bschade\b", "procedures", "financiële bepalingen", "nieuw", "Ob kostenverhaal, heffingen, schade; \\b voorkomt 'ontheffing'"),
    ("R36", r"gegevensbeheer|gegevensverzameling|persoonsgegevens|digitaal stelsel|aerius|^registers$|^monitoring en informatie$|^kaarten$|adviesorga|omgevingsdienst",
     "procedures", "gegevens en informatievoorziening", "nieuw", "Ob/Bkl"),
    ("R37", r"voorkeursrecht|onteigening|ruilbesluit|landinrichting", "planologisch gebruik", "grondbeleid", "nieuw", "Ob hfd 7-9, Bal landinrichting"),
    ("R38", r"projectbesluit|maatwerk en andere decentrale|aanwijzing van locaties voor rijkstaken|aanwijzing van rijkswateren|ontheffing",
     "procedures", "procedures", "", "bestaande subcategorie"),
]
# ---------------------------------------------------------------------------

origineel = (set(bi.CONTAINER), list(bi.REGEL_RE))

def indeel(pad, met_concept):
    if met_concept:
        bi.CONTAINER = origineel[0] | {bi.kern(c) for _, c, _ in CONTAINERS}
        bi.REGEL_RE = [(re.compile(p), c, s) for _, p, c, s, _, _ in REGELS] + origineel[1]
    else:
        bi.CONTAINER, bi.REGEL_RE = origineel
    label = bi.ruw_label(pad.split(" > ")) if pad else ""
    return label, bi.cureer(label)

def welke_regel(label):
    for rid, p, c, s, _, _ in REGELS:
        if re.search(p, label): return rid
    return "bestaand"

c = get_conn()
c.row_factory = tuple_row
c.execute("SET statement_timeout = '1800s'")
docs = dict(c.execute("SELECT frbr_expression, documenttype FROM p2p.regeling").fetchall())
namen = dict(c.execute("SELECT frbr_expression, opschrift FROM p2p.regeling").fetchall())
rows = c.execute(bi.PAD_SQL.format(seed_filter="")).fetchall()
print(len(rows), "artikelen")

def kort(t):
    for k, v in [("procedurele", "Ob"), ("regels over bouwwerken", "Bbl"), ("kwaliteit van de fysieke", "Bkl"),
                 ("activiteiten in de fysieke", "Bal"), ("Luchthavenbesluit", "Luchthavenbesluit Lelystad")]:
        if k in (t or ""): return v
    return (t or "")[:40]

paden, regressie, per_regel, per_container = {}, collections.Counter(), collections.Counter(), collections.Counter()
reg_voorbeelden = []
tel = collections.Counter()
for art, expr, wid, art_op, pad, _b in rows:
    dt = docs.get(expr)
    oud_label, oud = indeel(pad, False)
    nieuw_label, nieuw = indeel(pad, True)
    if dt == "AMvB":
        tel["amvb"] += 1
        tel["amvb oud ingedeeld"] += bool(oud)
        tel["amvb nieuw ingedeeld"] += bool(nieuw)
        if not oud:
            k = bi.pad_sleutel(pad)
            p = paden.setdefault(k, {"regeling": kort(namen.get(expr)), "pad": pad, "oud_label": oud_label,
                                     "nieuw_label": nieuw_label, "categorie": nieuw[0] if nieuw else None,
                                     "subcategorie": nieuw[1] if nieuw else None,
                                     "via": (welke_regel(nieuw_label) if nieuw else None),
                                     "container": next((cid for cid, cc, _ in CONTAINERS if bi.kern(cc) == oud_label), None),
                                     "n": 0, "voorbeelden": []})
            p["n"] += 1
            if len(p["voorbeelden"]) < 2 and art_op: p["voorbeelden"].append(art_op)
            if nieuw: per_regel[p["via"]] += 1
            if p["container"]: per_container[p["container"]] += 1
        elif oud != nieuw:
            tel["amvb al ingedeeld, verandert"] += 1
            reg_voorbeelden.append(("AMvB", kort(namen.get(expr)), pad, oud, nieuw))
    else:
        if oud != nieuw:
            regressie[dt] += 1
            reg_voorbeelden.append((dt, kort(namen.get(expr)), pad, oud, nieuw))

print(dict(tel))
print("regressie buiten AMvB:", dict(regressie))
json.dump({"paden": list(paden.values()), "per_regel": per_regel, "per_container": per_container,
           "tel": tel, "regressie": regressie,
           "regressie_voorbeelden": reg_voorbeelden,
           "containers": CONTAINERS, "regels": REGELS},
          open(os.path.join(os.path.dirname(__file__), "..", "logs", "concept_rijkspaden.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=list)
print("per regel:", per_regel.most_common())
print("nog leeg:", sum(p["n"] for p in paden.values() if not p["categorie"]), "art in",
      sum(1 for p in paden.values() if not p["categorie"]), "paden")
