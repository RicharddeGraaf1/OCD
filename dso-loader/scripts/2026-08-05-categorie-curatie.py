"""Curatie-ronde 2026-08-05 op v2a.categorie — beoordeling van de 78 discovery-voorstellen.

Waarom dit een script is en geen handmatige UPDATE-sessie: `build_categorie.py`
draait een DDL die `v2a.categorie` DROPt. Alles wat je met de hand in die tabel
zet is één herbouw verwijderd van weg — inclusief de 31 hernoemingen die er al
in stonden. Dit bestand is daarom de herhaalbare bron van de beslissingen; de
tabel is de afgeleide. Zie ook `export_curatie.py`, dat het resultaat wegschrijft
naar een JSON die `build_categorie.py` bij een herbouw weer inleest.

Vier soorten beslissingen:

  PROMOVEER  kandidaat wordt onderdeel van de toewijzings-ruggengraat.
             Nodig omdat stap 6 van build_categorie.py alleen aan `bevestigd`
             toewijst; een kandidaat krijgt daardoor per definitie nul chunks.
  VERPLAATS  kandidaat hangt onder het verkeerde thema. De argmax parkeert een
             cluster bij zijn sterkste buur, niet bij zijn juiste thema — dat is
             ook waarom `lucht` en `veiligheid` op nul staan terwijl er clusters
             van 302 resp. 49 chunks bestaan die er onmiskenbaar bij horen.
  HERNOEM    machinale top-3-TF-IDF-naam vervangen door een leesbare.
  VOEG_SAMEN twee of drie clusters van hetzelfde onderwerp. De overlevende
             krijgt het gewogen gemiddelde van de centroïden; de opgeslokte
             krijgt status 'afgekeurd' + merged_into.
  AFKEUREN   procedureel taalgebruik zonder onderwerp.

Beslissingen genomen in overleg met de gebruiker, 2026-08-05.

Idempotent: elke stap matcht op naam en eist exact één rij (geen stille no-ops).
Draaien:  python scripts/2026-08-05-categorie-curatie.py [--dry-run]
"""

import argparse
import io
import re
import sys

import numpy as np
import psycopg

DB = "postgresql://postgres:postgres@localhost:5434/dso"

# ── Beslissingen ─────────────────────────────────────────────────────────
# (huidige naam, nieuwe naam of None, nieuw thema of None, promoveren?)

PROMOVEER = [
    # kandidaat,                                      nieuw thema, nieuwe naam
    ("Aanvraagvereisten monumenten en archeologie",   "erfgoed",   None),
    ("Monumenten (voorbescherming)",                  "erfgoed",   None),
    ("Milieubelastende activiteiten (algemeen)",      "milieu",    None),
    ("Tanken en vloeibare brandstoffen",              "milieu",    None),
    ("Energietransitie en klimaat",                   "energie",   None),
    ("Slagschaduw van windturbines",                  "energie",   None),
    ("Circulaire economie en toekomstvisie",          "economie",  None),
    # Geur van mest hoort bij landbouw, niet bij milieu (gebruiker 2026-08-05),
    # en gaat samen met veehouderij — zie VOEG_SAMEN hieronder.
    ("Veehouderij en geuremissie",                    "landbouw",  "Veehouderij, mestopslag en geuremissie"),
    # Verplaatsen alleen is niet genoeg: een `kandidaat` doet niet mee in de
    # toewijzing, dus `lucht` en `veiligheid` zouden ondanks de verhuizing op
    # nul blijven staan. Deze vier zijn precies de clusters die die thema's
    # inhoud geven.
    ("Luchtemissie en stofmetingen",                  "lucht",     None),
    ("Concentratiemetingen (uurgemiddelden)",         "lucht",     None),
    ("Kwetsbare gebouwen (externe veiligheid)",       "veiligheid", None),
    ("Zonne-energie-opstellingen",                    "energie",   None),
]

# opgeslokte -> overlevende
VOEG_SAMEN = [
    ("Geur van mest- en agrarische opslag",           "Veehouderij, mestopslag en geuremissie"),
    ("watermeter / onttrekken grondwater / grondwater onttrokken",
                                                      "Grondwateronttrekking en boorputten"),
    ("boorput / geschikt onttrekken / grondwater uitwisseling",
                                                      "Grondwateronttrekking en boorputten"),
    ("omgevingsvergunning grondwater / grondwater onttrekken / onttrekkingsfilter",
                                                      "Grondwateronttrekking en boorputten"),
    ("goothoogte / alleen verleend / hart",            "Maatvoering: bouw- en goothoogte"),
    ("erosiebestendige / voorschriften gelden / aansluit bestaande", "Oeverinrichting"),
]

# huidige naam -> (nieuwe naam, nieuw thema of None)
HERNOEM = [
    # verkeerd thema — deze acht vullen meteen de lege thema's
    ("octaafband / waarneempunt / geluidvermogen",     "Geluidvermogen en waarneempunten", "geluid"),
    ("emissie lucht / deelmetingen / totaal stof",     "Luchtemissie en stofmetingen",     "lucht"),
    ("uurgemiddelde / uurgemiddelde concentraties / meten concentratie",
                                                       "Concentratiemetingen (uurgemiddelden)", "lucht"),
    ("gedragscode soortenbescherming / gedragscode / aardgasequivalent",
                                                       "Gedragscode soortenbescherming",   "natuur"),
    ("beperkt kwetsbare / kwetsbare kwetsbare / kwetsbare gebouwen",
                                                       "Kwetsbare gebouwen (externe veiligheid)", "veiligheid"),
    ("opstelling zonne / opstellingen zonne / zonne energie",
                                                       "Zonne-energie-opstellingen",       "energie"),
    ("faalkans / overstromings / overstromings faalkans",
                                                       "Overstromingskans en faalkans",    "water"),
    ("beheer afvalwater / geloosd vuilwaterriool / afvalwater lozen",
                                                       "Beheer en lozing van afvalwater",  "water"),
    # binnen eigen thema, alleen leesbaarder
    ("grotere diepte / plaatsvindt grotere / onttrekking plaatsvindt",
                                                       "Grondwateronttrekking en boorputten", None),
    ("sikb / ondergrondse opslagtank / erkenning bodemkwaliteit",
                                                       "Tankopslag en bodemerkenningen (SIKB)", None),
    ("dieper meter / boringsvrije / boringsvrije zone", "Boringsvrije zones",              None),
    ("subbrandcompartiment / vluchtroute / bouwconstructie",
                                                       "Brandveiligheid en vluchtroutes",  None),
    ("vloer / verbrandingslucht / rookgas",            "Verbrandingslucht en rookgasafvoer", None),
    ("meter bouwhoogte / maximaal meter / bouwhoogte", "Maatvoering: bouw- en goothoogte", None),
    ("faunabeheereenheid / faunabeheerplan / wildbeheereenheid", "Faunabeheer",             None),
    ("hectare eigendom / naam gebied / regio natuurbeheerplan", "Natuurbeheerplan",         None),
    ("ospar / deel noordzee / nederlandse deel",       "Noordzee en OSPAR",                None),
    ("aantasting kernkwaliteit / interpretatie toetsing / openheid",
                                                       "Kernkwaliteiten en openheid",      None),
    ("milieueffectrapport / plan programma / aanzienlijke milieueffecten",
                                                       "Milieueffectrapportage",           None),
    ("omgevingsvergunning aanvraag / meervoudige aanvraag / meer activiteiten",
                                                       "Meervoudige aanvraag",             None),
    ("duiker / damwand / vlonder",                     "Waterwerken: duikers, damwanden, vlonders", None),
    ("leiding zone / aanleggen kabel / mantelbuis",    "Kabels en leidingen",              None),
    ("hoeveelheid natuurvriendelijke / verloren hoeveelheid / oever plaatsvindt",
                                                       "Oeverinrichting",                  None),
]

AFKEUREN = [
    "verboden milieubelastende / verrichten zonder / melden verboden",
    "houdt specifieke / afmeerpaal / verholen",
    "niets nodig / nodig voldaan / bouwwerk zonder",
    "regels aangewezen / gebruiksfunctie leden / voldaan naleving",
    "voldaan toepassing / toepassing geven / gemeenteweg",
    "afdeling gegevens / gezag afdeling / verricht verwachte",
    "oordeel bevoegd / nevenfunctie / oordeel",
    "invulling behoefte / definitief aangewezen / voorkeursalternatief",
    "metalen paragraaf / tanken paragraaf / verricht voldaan",
    "leefomgeving vastgelegd / begrensd bijlage / aangewezen geometrisch",
    "artikelen theatergebruik / theatergebruik / bufferbewaarplaats",
    "inspecteur / selecteer / inspecteur vaststellen",
    "inwerkingtreding verordening / activiteit inwerkingtreding / melding activiteit",
    "plaatsvindt beperkingengebied / beperkingengebied regionale / waterkering beperkingengebied",
    "aanvangs / geplande aanvangs / aanvangs einddatum",
    "behouden halen / halen gebied / gebied kernzone",
    "begrenzing beperkingengebied / verordening geometrische / informatieobject",
    "voldaan specifieke / hoogheemraadschap voldaan / zorgplicht artikelen",
    "mede verstaan / uitvoeren graafwerkzaamheden / verstaan verplaatsen",
]

# Bewust NIET gepromoveerd, wel behouden als label: de twee waterclusters met
# n=98.549 en n=16.066. Die zijn een orde van grootte groter dan de op twee na
# grootste (463) en zijn geen onderwerp maar de bulk-boilerplate van
# waterschapsregels. Als seed zouden ze `water` nog verder laten slokken.
NIET_PROMOVEREN = [
    "Lozen van afvalwater, grondwater en hemelwater",
    "Activiteiten bij waterkeringen en waterstaatswerken",
]


def een(cur, naam: str, mag_ontbreken: bool = False) -> str | None:
    """Categorie-id bij een naam; eist exact één treffer.

    `mag_ontbreken` maakt het script herhaalbaar: na een geslaagde run bestaan
    de oude, machinale namen niet meer. Nul treffers is dan geen fout maar
    "al gedaan" — mits de nieuwe naam er wél is, wat de aanroeper controleert.
    """
    cur.execute("SELECT categorie_id FROM v2a.categorie WHERE naam = %s", (naam,))
    rijen = cur.fetchall()
    if not rijen and mag_ontbreken:
        return None
    if len(rijen) != 1:
        raise SystemExit(f"FOUT: {len(rijen)} treffers voor {naam!r} — verwacht 1")
    return rijen[0][0]


def thema_id(cur, naam: str) -> str:
    cur.execute(
        "SELECT categorie_id FROM v2a.categorie WHERE naam = %s AND parent_id IS NULL",
        (naam,),
    )
    rijen = cur.fetchall()
    if len(rijen) != 1:
        raise SystemExit(f"FOUT: thema {naam!r} niet uniek gevonden ({len(rijen)})")
    return rijen[0][0]


def main(dry: bool) -> None:
    conn = psycopg.connect(DB)
    cur = conn.cursor()
    n = {"hernoem": 0, "verplaats": 0, "promoveer": 0, "samen": 0, "afkeur": 0}

    # 1. Hernoemen + eventueel verplaatsen
    for oud, nieuw, thema in HERNOEM:
        cid = een(cur, oud, mag_ontbreken=True)
        if cid is None:
            een(cur, nieuw)          # al hernoemd in een eerdere run
            continue
        if thema:
            cur.execute(
                "UPDATE v2a.categorie SET naam=%s, parent_id=%s WHERE categorie_id=%s",
                (nieuw, thema_id(cur, thema), cid),
            )
            n["verplaats"] += 1
        else:
            cur.execute("UPDATE v2a.categorie SET naam=%s WHERE categorie_id=%s", (nieuw, cid))
        n["hernoem"] += 1

    # 2. Promoveren tot ruggengraat (hernoemt ook, dus vóór het samenvoegen)
    for naam, thema, nieuwe_naam in PROMOVEER:
        # Ook hier hernoemen we soms, dus bij een tweede run bestaat de oude
        # naam niet meer; val dan terug op de nieuwe.
        cid = een(cur, naam, mag_ontbreken=True) or (een(cur, nieuwe_naam) if nieuwe_naam else None)
        if cid is None:
            raise SystemExit(f"FOUT: {naam!r} niet gevonden en geen nieuwe naam opgegeven")
        cur.execute(
            """UPDATE v2a.categorie
               SET status='bevestigd', bron='gebruiker', parent_id=%s,
                   naam=coalesce(%s, naam)
               WHERE categorie_id=%s""",
            (thema_id(cur, thema), nieuwe_naam, cid),
        )
        n["promoveer"] += 1

    # 3. Samenvoegen — overlevende krijgt het gewogen gemiddelde van de
    #    centroïden, gewogen op clustergrootte: een cluster van 190 hoort het
    #    gemiddelde meer te bepalen dan een van 45.
    #
    #    In Python en niet in SQL: pgvector kent geen `vector * integer`. En na
    #    het middelen normaliseren, want de toewijzing in build_categorie.py
    #    rekent cosine af als dot-product op eenheidsvectoren — een niet-genormeerde
    #    centroïde zou daar stelselmatig te zwak of te sterk in wegen.
    for opgeslokt, overlevende in VOEG_SAMEN:
        a, b = een(cur, opgeslokt, mag_ontbreken=True), een(cur, overlevende)
        if a is None:
            continue                 # opgeslokte bestaat niet meer
        cur.execute("SELECT merged_into FROM v2a.categorie WHERE categorie_id=%s", (a,))
        if (cur.fetchone() or [None])[0]:
            continue                 # al samengevoegd
        cur.execute(
            "SELECT categorie_id, centroide::text, coalesce(n_chunks_seen,0) "
            "FROM v2a.categorie WHERE categorie_id IN (%s,%s)", (a, b))
        vec = {cid: (np.fromstring(c.strip("[]"), sep=",", dtype=np.float32), n)
               for cid, c, n in cur.fetchall()}
        va, na = vec[a]
        vb, nb = vec[b]
        gem = (va * na + vb * nb) / max(na + nb, 1)
        gem = gem / (np.linalg.norm(gem) + 1e-9)
        lit = "[" + ",".join(f"{x:.6f}" for x in gem) + "]"
        cur.execute(
            "UPDATE v2a.categorie SET centroide=%s::vector, n_chunks_seen=%s WHERE categorie_id=%s",
            (lit, na + nb, b))
        cur.execute(
            "UPDATE v2a.categorie SET status='afgekeurd', merged_into=%s WHERE categorie_id=%s",
            (b, a))
        n["samen"] += 1

    # 4. Afkeuren
    for naam in AFKEUREN:
        cid = een(cur, naam)
        cur.execute("UPDATE v2a.categorie SET status='afgekeurd' WHERE categorie_id=%s", (cid,))
        n["afkeur"] += 1

    if dry:
        conn.rollback()
        print("DRY-RUN — teruggedraaid.")
    else:
        conn.commit()
    print(f"hernoemd {n['hernoem']} (waarvan verplaatst {n['verplaats']}), "
          f"samengevoegd {n['samen']}, gepromoveerd {n['promoveer']}, afgekeurd {n['afkeur']}")
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    main(p.parse_args().dry_run)
