"""Backfill: kies bij meerdere gebiedsmarkeringen de kandidaat die bij het
huisnummer van de publicatie hoort (vervolg op gaps G-87).

HET PROBLEEM
------------
`backfill_geometrie.py` (G-87, juli 2026) corrigeerde grove misplaatsingen: een
Amsterdams adres met een pin in Woerden. Die selectie grijpt bewust pas in bij
meer dan 1 km onderlinge afstand (`_DIVERGENCE_M`), om geen churn te maken op
de ~163k records waar punt en omhullend vlak dicht bij elkaar liggen.

Daardoor bleef een hele klasse fouten staan: het verkeerde pand in hetzelfde
bouwblok. KOOP levert bij blok-zaken één markering per pand — gmb-2024-503409
heeft er 16 — en wij namen `cands[0]`. Op wijkniveau is dat precies wat je ziet
als je je eigen straat opzoekt.

Gemeten in postcodegebied 1097, 80 records met meerdere kandidaten >10 m
uiteen: 6 hadden de dichtste al, bij 22 lag een andere kandidaat >25 m
dichterbij. Voorbeeld gmb-2024-377279 ("Veeteeltstraat 20"): drie markeringen,
wij sloegen die op 127 m van nummer 20 op, terwijl de tweede markering exact op
het BAG-punt van nummer 20 ligt.

DE AANPAK
---------
Adres van het record geocoderen (PDOK, gecached in vth.adres_geocode) en
daarna de dichtstbijzijnde **door de bronhouder aangeleverde** kandidaat
kiezen. Het BAG-punt zelf wordt nooit opgeslagen: het register hoort te tonen
wat het bevoegd gezag publiceerde, niet wat wij eruit afleiden.

Elke wijziging gaat naar `vth.geometrie_correctie` met de oude waarden erbij,
zodat de ronde te inspecteren en terug te draaien is.

GEBRUIK
-------
    # proefdraaien op één wijk, schrijft niets:
    python scripts/koop-poc/backfill_geometrie_adres.py --postcode 1097

    # daadwerkelijk toepassen:
    python scripts/koop-poc/backfill_geometrie_adres.py --postcode 1097 --apply

    # terugdraaien wat deze ronde deed:
    python scripts/koop-poc/backfill_geometrie_adres.py --rollback

Zonder `--postcode` loopt hij over de hele dataset; doe dat pas na een proef.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db import get_conn  # noqa: E402
from src.loaders.adres_geocode import (  # noqa: E402
    MIN_WINST_M, Geocoder, bouw_vraag, kies_op_adres, zorg_voor_cache,
)
from src.loaders.koop_vergunning import NS, _extract_candidate  # noqa: E402

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS vth.geometrie_correctie (
    koop_id          text PRIMARY KEY,
    oud_type         text,
    oud_wgs          geometry(Point, 4326),
    oud_rd           geometry(Point, 28992),
    nieuw_type       text,
    nieuw_wgs        geometry(Point, 4326),
    nieuw_rd         geometry(Point, 28992),
    verschuiving_m   double precision,
    afstand_voor_m   double precision,
    afstand_na_m     double precision,
    adres_vraag      text,
    gecorrigeerd_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE vth.geometrie_correctie IS
  'Audit van backfill_geometrie_adres.py: welke pin is verplaatst, van waar '
  'naar waar, en hoeveel dichter bij het eigen adres. Bevat de oude waarden '
  'zodat een ronde terug te draaien is.';
"""

UPDATE_SQL = """
UPDATE vth.vergunningkennisgeving SET
    geometrie_type  = %(gt)s,
    geometrie_rd_pt = ST_GeomFromText(%(rdpt)s::text, 28992),
    geometrie_wgs_pt = ST_GeomFromText(%(wgspt)s::text, 4326)
WHERE koop_id = %(id)s
"""

AUDIT_SQL = """
INSERT INTO vth.geometrie_correctie
    (koop_id, oud_type, oud_wgs, oud_rd, nieuw_type, nieuw_wgs, nieuw_rd,
     verschuiving_m, afstand_voor_m, afstand_na_m, adres_vraag)
VALUES (%(id)s, %(oud_type)s,
        ST_GeomFromText(%(oud_wgs)s::text, 4326),
        ST_GeomFromText(%(oud_rd)s::text, 28992),
        %(gt)s,
        ST_GeomFromText(%(wgspt)s::text, 4326),
        ST_GeomFromText(%(rdpt)s::text, 28992),
        %(verschuiving)s, %(voor)s, %(na)s, %(vraag)s)
ON CONFLICT (koop_id) DO UPDATE SET
    nieuw_type = EXCLUDED.nieuw_type, nieuw_wgs = EXCLUDED.nieuw_wgs,
    nieuw_rd = EXCLUDED.nieuw_rd, verschuiving_m = EXCLUDED.verschuiving_m,
    afstand_voor_m = EXCLUDED.afstand_voor_m, afstand_na_m = EXCLUDED.afstand_na_m,
    adres_vraag = EXCLUDED.adres_vraag, gecorrigeerd_at = now()
"""

SELECT_SQL = """
SELECT koop_id, titel, raw_xml, geometrie_type,
       straatnaam, huisnummer, huisletter, huisnummertoevoeging,
       postcode, woonplaats, ligt_in_gemeente,
       ST_X(geometrie_rd_pt)  AS cur_x,  ST_Y(geometrie_rd_pt)  AS cur_y,
       ST_X(geometrie_wgs_pt) AS cur_lon, ST_Y(geometrie_wgs_pt) AS cur_lat
FROM vth.vergunningkennisgeving
WHERE raw_xml IS NOT NULL
  AND huisnummer IS NOT NULL
  AND straatnaam IS NOT NULL
  AND regexp_count(raw_xml, '<([A-Za-z0-9]+:)?gebiedsmarkering') >= 2
"""


def verzamel(conn, geocoder: Geocoder, postcode: str | None, maximum: int | None):
    """Fase 1 — read-only: bepaal welke records een betere kandidaat hebben."""
    sql = SELECT_SQL
    params: list = []
    if postcode:
        # Ook op huisnummertoevoeging matchen: bij een deel van de records is de
        # postcode dáár beland en is de postcode-kolom leeg (gmb-2024-377279).
        # Precies zulke records zijn kandidaat voor correctie, dus ze uit de
        # selectie laten vallen zou het interessantste geval overslaan.
        sql += " AND (postcode LIKE %s OR huisnummertoevoeging LIKE %s)"
        pat = postcode.rstrip("%") + "%"
        params.extend([pat, pat])
    sql += " ORDER BY koop_id"
    if maximum:
        sql += f" LIMIT {int(maximum)}"

    lees = get_conn()  # aparte leesconnectie: de geocoder commit op de zijne
    cur = lees.cursor()
    wijzigingen: list[dict] = []
    stats = {"bekeken": 0, "geen_adres": 0, "geen_geocode": 0,
             "al_goed": 0, "te_klein": 0, "buiten_bereik": 0}
    try:
        for rij in cur.stream(sql, params) if not maximum else cur.execute(sql, params).fetchall():
            stats["bekeken"] += 1
            vraag = bouw_vraag(rij["straatnaam"], rij["huisnummer"], rij["huisletter"],
                               rij["huisnummertoevoeging"], rij["postcode"],
                               rij["woonplaats"], rij["ligt_in_gemeente"])
            if not vraag:
                stats["geen_adres"] += 1
                continue
            try:
                root = ET.fromstring(rij["raw_xml"])
            except ET.ParseError:
                continue
            cands = []
            for geb in root.findall(".//ow:gebiedsmarkering", NS):
                for kind in list(geb):
                    c = _extract_candidate(kind)
                    if c:
                        cands.append(c)
            if len(cands) < 2:
                continue

            geo = geocoder(vraag)
            if not geo:
                stats["geen_geocode"] += 1
                continue

            huidig = None
            if rij["cur_x"] is not None:
                huidig = (rij["cur_x"], rij["cur_y"])
            beste, voor, na = kies_op_adres(cands, geo, huidig)
            if beste is None:
                stats["buiten_bereik"] += 1
                continue
            if voor is None:
                winst = None
            else:
                winst = voor - na
                if winst < MIN_WINST_M:
                    stats["al_goed" if winst <= 1 else "te_klein"] += 1
                    continue

            verschuiving = None
            if huidig is not None:
                verschuiving = ((beste["rd_x"] - huidig[0]) ** 2
                                + (beste["rd_y"] - huidig[1]) ** 2) ** 0.5
            wijzigingen.append({
                "id": rij["koop_id"], "gt": beste["geometrie_type"],
                "rdpt": f"POINT({beste['rd_x']} {beste['rd_y']})",
                "wgspt": f"POINT({beste['lon']} {beste['lat']})"
                         if beste.get("lon") is not None else None,
                "oud_type": rij["geometrie_type"],
                "oud_rd": f"POINT({rij['cur_x']} {rij['cur_y']})" if rij["cur_x"] is not None else None,
                "oud_wgs": f"POINT({rij['cur_lon']} {rij['cur_lat']})" if rij["cur_lon"] is not None else None,
                "verschuiving": verschuiving, "voor": voor, "na": na,
                "vraag": vraag, "titel": rij["titel"],
            })
    finally:
        lees.close()
    return wijzigingen, stats


def toepassen(wijzigingen: list[dict]) -> int:
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    for w in wijzigingen:
        if not w["wgspt"]:
            continue
        cur.execute(UPDATE_SQL, w)
        cur.execute(AUDIT_SQL, w)
        n += 1
        if n % 500 == 0:
            conn.commit()
            print(f"    …{n:,}/{len(wijzigingen):,}", flush=True)
    conn.commit()
    conn.close()
    return n


def terugdraaien() -> int:
    """Zet elke gecorrigeerde pin terug op zijn oude waarde."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE vth.vergunningkennisgeving v SET
            geometrie_type   = c.oud_type,
            geometrie_rd_pt  = c.oud_rd,
            geometrie_wgs_pt = c.oud_wgs
        FROM vth.geometrie_correctie c
        WHERE v.koop_id = c.koop_id AND c.oud_wgs IS NOT NULL
    """)
    n = cur.rowcount
    cur.execute("DELETE FROM vth.geometrie_correctie")
    conn.commit()
    conn.close()
    return n


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--postcode", help="beperk tot postcodes die hiermee beginnen, bv. 1097")
    p.add_argument("--limit", type=int, help="maximaal aantal records bekijken")
    p.add_argument("--apply", action="store_true", help="wijzigingen wegschrijven")
    p.add_argument("--rollback", action="store_true", help="vorige ronde terugdraaien")
    args = p.parse_args()

    conn = get_conn()
    zorg_voor_cache(conn)
    with conn.cursor() as cur:
        cur.execute(AUDIT_DDL)
    conn.commit()

    if args.rollback:
        n = terugdraaien()
        print(f"Teruggedraaid: {n:,} records hersteld naar hun oude geometrie.")
        conn.close()
        return

    geocoder = Geocoder(conn)
    bereik = f"postcode {args.postcode}*" if args.postcode else "HELE DATASET"
    print(f"Fase 1 — kandidaten herwegen ({bereik})…", flush=True)
    wijzigingen, stats = verzamel(conn, geocoder, args.postcode, args.limit)

    print(f"\nBekeken records met >=2 markeringen : {stats['bekeken']:,}")
    print(f"  geen bruikbaar adres              : {stats['geen_adres']:,}")
    print(f"  adres niet gevonden bij PDOK      : {stats['geen_geocode']:,}")
    print(f"  keuze was al de dichtste          : {stats['al_goed']:,}")
    print(f"  winst < {MIN_WINST_M:.0f} m, niet aangeraakt      : {stats['te_klein']:,}")
    print(f"  geen kandidaat binnen bereik      : {stats['buiten_bereik']:,}")
    print(f"  TE CORRIGEREN                     : {len(wijzigingen):,}")
    print(f"\ngeocache: {geocoder.uit_cache:,} uit cache, "
          f"{geocoder.opgehaald:,} opgehaald, {geocoder.niet_gevonden:,} niet gevonden")

    if wijzigingen:
        gem = sum(w["voor"] - w["na"] for w in wijzigingen if w["voor"]) / len(wijzigingen)
        print(f"gemiddelde winst: {gem:.0f} m dichter bij het eigen adres")
        print("\nvoorbeelden:")
        for w in sorted(wijzigingen, key=lambda x: -(x["voor"] or 0))[:12]:
            print(f"  {w['id']:<18} {w['voor']:>6.0f} m -> {w['na']:>4.0f} m   {w['titel'][:58]}")

    if args.apply and wijzigingen:
        print(f"\nFase 2 — {len(wijzigingen):,} records bijwerken…", flush=True)
        n = toepassen(wijzigingen)
        print(f"Klaar: {n:,} bijgewerkt (audit in vth.geometrie_correctie).")
    elif not args.apply:
        print("\n(dry-run — draai met --apply om dit weg te schrijven)")
    conn.close()


if __name__ == "__main__":
    main()
