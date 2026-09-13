#!/usr/bin/env python
"""Vul de kaartlagen aan die op prod ontbreken (runbook §3a).

Waarom dit bestaat
------------------
`diff_lokaal_prod.py` meldde op 2026-09-13 een verschil van 170 rijen op
`p2p.kaartlaag` (1.154 lokaal, 984 op prod) dat al vóór die sync bestond. Geen
wezen aan beide kanten, en alle 22 betrokken `p2p.kaart`-ouders stáán op prod --
het waren dus geen kapotte verwijzingen maar simpelweg rijen die er nooit
gekomen zijn.

De oorzaak zit in de scope van `repliceer_p2p_naar_prod.py`:

    scope_kaart = kaart_id van elke kaartlaag waarvan de activiteit,
                  gebiedsaanwijzing of norm in scope zit
    kaartlaag   = alle lagen van een kaart in scope_kaart

Die tweede regel is ruim -- een kaart met één laag in scope neemt zijn andere
lagen mee. Maar een kaart waarvan in déze run géén enkele laag in scope valt,
komt helemaal niet in `scope_kaart`, en dan blijft een laag die later is
toegevoegd voor altijd achter. Precies zoals bij `p2p.divisie` reist een
backfill niet mee met een delta die op de expressies van de dag scoopt.

Wat het doet
------------
Kopieert de kaartlagen die lokaal bestaan en op prod niet, mits hun `p2p.kaart`
aan de prod-kant bestaat -- een ontbrekende ouder is een ander probleem en hoort
niet stilzwijgend meegekopieerd te worden (dezelfde regel als in
`backfill_tekstdeel_junctions_prod.py`).

`kaartlaag.id` is `GENERATED ALWAYS`, dus `OVERRIDING SYSTEM VALUE`: prod nieuwe
id's laten uitdelen maakt elke latere vergelijking tussen de twee kanten
onbruikbaar.

    python scripts/backfill_kaartlaag_prod.py          # droogloop
    python scripts/backfill_kaartlaag_prod.py --ja     # echt
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_conn  # noqa: E402

KOLOMMEN = ["id", "kaart_id", "naam", "gebiedsaanwijzing_id", "norm_id", "activiteit_id"]


def main() -> int:
    droog = "--ja" not in sys.argv
    prod_dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2

    lok = get_conn()
    lc = lok.cursor()
    lc.execute(f"SELECT {', '.join(KOLOMMEN)} FROM p2p.kaartlaag")
    lokaal = {r["id"]: r for r in lc.fetchall()}

    with psycopg.connect(prod_dsn, row_factory=dict_row, connect_timeout=30) as p:
        pc = p.cursor()
        pc.execute("SELECT id FROM p2p.kaartlaag")
        op_prod = {r["id"] for r in pc.fetchall()}
        ontbreekt = [r for i, r in lokaal.items() if i not in op_prod]
        print(f"lokaal {len(lokaal)} · prod {len(op_prod)} · ontbreekt {len(ontbreekt)}")
        if not ontbreekt:
            print("niets te doen")
            return 0

        ouders = sorted({r["kaart_id"] for r in ontbreekt})
        pc.execute("SELECT identificatie FROM p2p.kaart WHERE identificatie = ANY(%s)",
                   (ouders,))
        aanwezig = {r["identificatie"] for r in pc.fetchall()}
        wees = [r for r in ontbreekt if r["kaart_id"] not in aanwezig]
        te_doen = [r for r in ontbreekt if r["kaart_id"] in aanwezig]
        print(f"  betrokken kaarten: {len(ouders)}, daarvan op prod: {len(aanwezig)}")
        if wees:
            print(f"  OVERGESLAGEN: {len(wees)} lagen zonder kaart op prod "
                  f"— dat is een ander gat, niet dit script")

        if droog:
            print(f"\nDROOGLOOP — {len(te_doen)} rijen zouden worden ingevoegd. "
                  f"Draai opnieuw met --ja.")
            return 0

        kols = ", ".join(KOLOMMEN)
        with p.transaction():
            pc.executemany(
                f"INSERT INTO p2p.kaartlaag ({kols}) OVERRIDING SYSTEM VALUE "
                f"VALUES ({', '.join(['%s'] * len(KOLOMMEN))}) ON CONFLICT (id) DO NOTHING",
                [[r[k] for k in KOLOMMEN] for r in te_doen])
            # De sequence moet mee, anders deelt prod bij de volgende eigen
            # INSERT een id uit dat hier al gebruikt is.
            pc.execute("SELECT setval(pg_get_serial_sequence('p2p.kaartlaag', 'id'), "
                       "(SELECT max(id) FROM p2p.kaartlaag))")
        pc.execute("SELECT count(*) AS n FROM p2p.kaartlaag")
        print(f"klaar — prod staat nu op {pc.fetchone()['n']} kaartlagen "
              f"(lokaal {len(lokaal)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
