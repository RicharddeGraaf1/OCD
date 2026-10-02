"""Zet de gebiedsmarkering-reparatie over naar productie.

Hoort bij `backfill_gebiedsmarkering.py`, dat lokaal 41.049 records repareerde
die als "geen geometrie" in de DB stonden terwijl de WKT in hun eigen raw_xml
zat. Die reparatie raakt geen enkele nieuwe expressie en komt dus nooit langs de
vth-watermark of de p2p-delta — precies het geval uit runbook §3a: wie een
backfill draait, levert de overzetstap mee.

**Waarom pushen en niet de backfill tegen prod draaien.** Prod heeft de raw_xml
óók (925.329 van 925.329), dus `OCD_DB_URL=<prod> backfill_gebiedsmarkering.py
--apply` zou werken. Maar dat trekt 208 MB XML over de TCP-proxy naar Python,
tegen 16 MB geometrie als je de uitkomst van lokaal overzet. Gemeten op de
41.152-groep lokaal: xml 208 MB (gem. 5.294 bytes/record), geometrie 16 MB.

De CHECK op `geometrie_type` wordt eerst verbreed — prod kent nog de vier oude
waarden en zou op `Weg`/`Perceel`/`Lijn`/… afketsen. Verbreden kan bestaande
rijen niet afkeuren, dus dat is veilig vooruit.

Gebruik:
    python scripts/backfill_gebiedsmarkering_prod.py            # droogloop
    python scripts/backfill_gebiedsmarkering_prod.py --ja
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import get_conn  # noqa: E402

GEOM_TYPES = (
    "'Adres', 'Punt', 'Vlak', 'Postcodegebied', 'PostcodeHuisnummer', "
    "'GeometrieRef', 'Weg', 'Perceel', 'Lijn', 'Woonplaats', "
    "'Buurt', 'Wijk', 'Hectometerpaal'"
)

ALTER_CHECK = f"""
ALTER TABLE vth.vergunningkennisgeving
  DROP CONSTRAINT IF EXISTS vergunningkennisgeving_geometrie_type_check;
ALTER TABLE vth.vergunningkennisgeving
  ADD CONSTRAINT vergunningkennisgeving_geometrie_type_check
  CHECK (geometrie_type IS NULL OR geometrie_type IN ({GEOM_TYPES}));
"""

# De elf kolommen die de lokale backfill schrijft, in dezelfde volgorde.
KOLOMMEN = [
    "koop_id", "geometrie_type", "geometrie_rd", "geometrie_rd_pt",
    "geometrie_wgs_pt", "geometrielabel", "postcode", "huisnummer",
    "straatnaam", "woonplaats", "ligt_in_gemeente",
]

STAGING = """
CREATE TEMP TABLE _gm_stage (
  koop_id          TEXT PRIMARY KEY,
  geometrie_type   TEXT,
  geometrie_rd     GEOMETRY(GEOMETRY, 28992),
  geometrie_rd_pt  GEOMETRY(POINT, 28992),
  geometrie_wgs_pt GEOMETRY(POINT, 4326),
  geometrielabel   TEXT,
  postcode         TEXT,
  huisnummer       TEXT,
  straatnaam       TEXT,
  woonplaats       TEXT,
  ligt_in_gemeente TEXT
) ON COMMIT DROP
"""

# COALESCE op de tekstvelden, net als de lokale backfill: een waarde die prod al
# heeft wordt niet overschreven. De geometrie wél — die is daar immers NULL.
UPDATE_FROM = """
UPDATE vth.vergunningkennisgeving v SET
  geometrie_type   = s.geometrie_type,
  geometrie_rd     = s.geometrie_rd,
  geometrie_rd_pt  = s.geometrie_rd_pt,
  geometrie_wgs_pt = s.geometrie_wgs_pt,
  geometrielabel   = COALESCE(v.geometrielabel, s.geometrielabel),
  postcode         = COALESCE(v.postcode, s.postcode),
  huisnummer       = COALESCE(v.huisnummer, s.huisnummer),
  straatnaam       = COALESCE(v.straatnaam, s.straatnaam),
  woonplaats       = COALESCE(v.woonplaats, s.woonplaats),
  ligt_in_gemeente = COALESCE(v.ligt_in_gemeente, s.ligt_in_gemeente)
FROM _gm_stage s
WHERE v.koop_id = s.koop_id
  AND v.geometrie_rd IS NULL
"""


def prod_url() -> str:
    url = os.environ.get("PROD_DB_URL")
    if url:
        return url
    env = (Path(__file__).resolve().parents[1] / ".env").read_text(
        encoding="utf-8", errors="replace")
    m = re.search(r"^PROD_DB_URL=(.+)$", env, re.M)
    if not m:
        raise SystemExit("PROD_DB_URL staat niet in de omgeving en niet in .env")
    return m.group(1).strip().strip('"').strip("'")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ja", action="store_true", help="schrijf echt naar prod")
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()

    lok = get_conn()
    lcur = lok.cursor()
    lcur.execute("""SELECT count(*) n FROM vth.vergunningkennisgeving
                     WHERE geometrie_rd IS NOT NULL""")
    n_lokaal = list(lcur.fetchone().values())[0]

    with psycopg.connect(prod_url(), connect_timeout=30) as prod:
        pcur = prod.cursor()
        pcur.execute("SELECT current_database()")
        db = pcur.fetchone()[0]
        if db != "railway":
            raise SystemExit(f"doel-DB is '{db}', niet 'railway' — gestopt")
        pcur.execute("""SELECT count(*) FROM vth.vergunningkennisgeving
                         WHERE geometrie_rd IS NULL""")
        n_leeg = pcur.fetchone()[0]
        print(f"doel: {db} · prod zonder geometrie: {n_leeg} · lokaal mét: {n_lokaal}")

        # Welke koop_id's op prod leeg zijn én lokaal een geometrie hebben.
        pcur.execute("""SELECT koop_id FROM vth.vergunningkennisgeving
                         WHERE geometrie_rd IS NULL""")
        leeg = [r[0] for r in pcur.fetchall()]
        print(f"kandidaten van prod: {len(leeg)}")

        if not args.ja:
            lcur.execute("""SELECT count(*) n FROM vth.vergunningkennisgeving
                             WHERE koop_id = ANY(%s) AND geometrie_rd IS NOT NULL""",
                         (leeg,))
            print(f"droogloop: {list(lcur.fetchone().values())[0]} daarvan zijn lokaal "
                  f"gerepareerd en zouden worden overgezet")
            print("geen --ja meegegeven; niets geschreven")
            return

        print("CHECK verbreden op prod...")
        pcur.execute(ALTER_CHECK)
        prod.commit()

        pcur.execute(STAGING)
        t0 = time.time()
        n_over = 0
        kolomlijst = ", ".join(KOLOMMEN)
        for i in range(0, len(leeg), args.batch):
            brok = leeg[i:i + args.batch]
            lcur.execute(f"""SELECT {kolomlijst} FROM vth.vergunningkennisgeving
                              WHERE koop_id = ANY(%s) AND geometrie_rd IS NOT NULL""",
                         (brok,))
            rijen = lcur.fetchall()
            if not rijen:
                continue
            with pcur.copy(f"COPY _gm_stage ({kolomlijst}) FROM STDIN") as cp:
                for r in rijen:
                    cp.write_row([r[k] for k in KOLOMMEN])
            n_over += len(rijen)
            print(f"  {i + len(brok)}/{len(leeg)} bekeken · {n_over} in staging "
                  f"· {time.time() - t0:.0f}s")

        pcur.execute(UPDATE_FROM)
        n_upd = pcur.rowcount
        prod.commit()
        print(f"bijgewerkt op prod: {n_upd} rijen in {time.time() - t0:.0f}s")

        pcur.execute("""SELECT count(*) tot, count(geometrie_rd) met_geo
                          FROM vth.vergunningkennisgeving""")
        print("prod na de backfill:", pcur.fetchone())
    lok.close()


if __name__ == "__main__":
    main()
