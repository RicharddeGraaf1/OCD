#!/usr/bin/env python
"""Trek gedrifte locatiegeometrieen op prod gelijk met lokaal (vault G-142).

Waarom dit bestaat
------------------
`p2p.locatie` heeft aan beide kanten evenveel rijen en dezelfde sleutels, en
tóch een andere inhoud: op 2026-09-13 droegen **219 locaties over 41
bronhouders** dezelfde `identificatie` met een andere geometrie.

De oorzaak zit in de scope van `repliceer_p2p_naar_prod.py`. Die upsert alleen
locaties die vanuit de geladen expressies bereikbaar zijn. Wijzigt een geometrie
terwijl haar locatie in die run buiten scope ligt -- bijvoorbeeld omdat de
regeling die haar aanwijst niet opnieuw is geladen -- dan blijft prod de oude
houden. Voor altijd, want geen enkele volgende run kijkt er meer naar.

Het is zichtbaar werk en geen boekhouding: `p2p.locatie_subdiv` wordt per
bronhouder over alle polygoonlocaties gebouwd, `p2p.locatie_generalisatie` is
daarvan afgeleid en `ocd-api/tiles.py` leest dat. Een verkeerde geometrie op
prod staat dus gewoon op de kaart.

Deze controle zit sinds 2026-09-05 in `diff_lokaal_prod.py` als de
geometrie-hash per bronhouder; dit script is de bijbehorende reparatie, die er
tot nu toe niet was.

Na afloop moeten `locatie_subdiv` en `locatie_generalisatie` opnieuw voor de
geraakte bronhouders -- het script schrijft die lijst weg, want dat is het dure
deel en dat plan je liever zelf.

    python scripts/herstel_locatie_geometrie_prod.py            # droogloop
    python scripts/herstel_locatie_geometrie_prod.py --ja       # echt
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn  # noqa: E402

HASH_SQL = ("SELECT identificatie, md5(ST_AsBinary(geometrie)) AS h "
            "FROM p2p.locatie WHERE geometrie IS NOT NULL")
# Let op de staart: zonder `.*` vervangt `sub` alleen het voorvoegsel en houd
# je "gm0119gebiedengroep.a8d9..." over in plaats van "gm0119".
BRONHOUDER_RE = re.compile(r"^nl\.imow-([a-z0-9]+)\..*$")


def main() -> int:
    droog = "--ja" not in sys.argv
    prod_dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2

    lok = get_conn()
    lc = lok.cursor()
    lc.execute(HASH_SQL)
    L = {r["identificatie"]: r["h"] for r in lc.fetchall()}

    with psycopg.connect(prod_dsn, row_factory=dict_row, connect_timeout=30) as p:
        pc = p.cursor()
        pc.execute(HASH_SQL)
        P = {r["identificatie"]: r["h"] for r in pc.fetchall()}

        anders = sorted(k for k in L if k in P and L[k] != P[k])
        bronhouders = Counter(BRONHOUDER_RE.sub(r"\1", k) for k in anders)
        print(f"locaties met geometrie: lokaal {len(L)}, prod {len(P)}")
        print(f"zelfde sleutel, andere geometrie: {len(anders)} "
              f"over {len(bronhouders)} bronhouders")
        for bh, n in bronhouders.most_common(8):
            print(f"    {bh:8} {n}")
        if not anders:
            print("niets te doen")
            return 0

        if droog:
            print(f"\nDROOGLOOP — {len(anders)} geometrieen zouden worden bijgewerkt. "
                  f"Draai opnieuw met --ja.")
            return 0

        # Per blok, met EWKT over de lijn. Geen COPY: het gaat om honderden rijen
        # en een UPDATE per sleutel is hier eenvoudiger te volgen dan een
        # staging-tabel. Bij duizenden zou dat andersom liggen.
        BLOK = 200
        bijgewerkt = 0
        for i in range(0, len(anders), BLOK):
            sleutels = anders[i:i + BLOK]
            lc.execute("SELECT identificatie, ST_AsEWKT(geometrie) AS ewkt "
                       "FROM p2p.locatie WHERE identificatie = ANY(%s)", (sleutels,))
            rijen = [(r["ewkt"], r["identificatie"]) for r in lc.fetchall()]
            with p.transaction():
                pc.executemany(
                    "UPDATE p2p.locatie SET geometrie = ST_GeomFromEWKT(%s) "
                    "WHERE identificatie = %s", rijen)
            bijgewerkt += len(rijen)
            print(f"  {bijgewerkt}/{len(anders)}", flush=True)

        pc.execute(HASH_SQL)
        P2 = {r["identificatie"]: r["h"] for r in pc.fetchall()}
        rest = [k for k in L if k in P2 and L[k] != P2[k]]
        print(f"klaar — {bijgewerkt} bijgewerkt, {len(rest)} verschillen over")

    lijst = ROOT / "data" / "geometrie-herstel-bronhouders.txt"
    lijst.parent.mkdir(parents=True, exist_ok=True)
    lijst.write_text("\n".join(sorted(bronhouders)), encoding="utf-8")
    print(f"\nBronhouders weggeschreven naar {lijst}")
    print("Herbouw daarvoor op prod, in deze volgorde:")
    print("  OCD_DB_URL=\"$PROD_DB_URL\" python -m src.cli refresh-subdiv -b <code>")
    print("  OCD_DB_URL=\"$PROD_DB_URL\" python scripts/vul_locatie_generalisatie.py --bronhouder <code> ...")
    print("Zonder die herbouw staat de oude geometrie nog steeds op de kaart:")
    print("locatie_subdiv en locatie_generalisatie zijn afgeleid en volgen niet vanzelf.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
