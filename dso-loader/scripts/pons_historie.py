"""Pons-geschiedenis per gemeente: de pons van het omgevingsplan sinds 2024-01-01.

Vult p2p.pons_historie (DDL: scripts/2026-11-12-add-pons-historie.sql). Leest de DSO
(Presenteren-API) met geldigOp en inWerkingOp op de 1e van elke maand vanaf 2024-01-01 tot
vandaag. Verandert de geometrieIdentificatie van de pons tussen twee peilingen, dan zoekt een
bisectie de eerste dag van de nieuwe pons. Per unieke geometrieIdentificatie één geometrie-call.

beschikbaarOp staat op nu: de vraag is wat er op die dag gold volgens wat we vandaag weten, niet
wat er toen bekend was.

Gebruik:
    python scripts/pons_historie.py --gm gm0302,gm0392 --droog          # alleen peilen, niets schrijven
    python scripts/pons_historie.py --gm gm0302,gm0392 --db lokaal
    python scripts/pons_historie.py --gm gm0302,gm0392 --db prod        # via de tunnel, PROD_DB_URL
    python scripts/pons_historie.py --gm gm0302,gm0392 --db beide       # één keer peilen, twee keer schrijven

Per gemeente zo'n 35 maandpeilingen plus ~5 per wijziging: klein genoeg voor overdag. Alle
gemeenten tegelijk is bulk (ruim 12.000 calls): alleen 22:00-06:00.

Het omgevingsplan (work-id) komt uit p2p.regeling van de lokale database.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as ocd_db  # noqa: E402
from src.loaders import api_loader as a  # noqa: E402

START = date(2024, 1, 1)
RE_GM = re.compile(r"^gm\d{4}$")
DDL = Path(__file__).with_name("2026-11-12-add-pons-historie.sql")


def lokale_conn() -> psycopg.Connection:
    os.environ.pop("OCD_DB_URL", None)
    return psycopg.connect(a.cfg.db_url, autocommit=False)


def prod_conn() -> psycopg.Connection:
    dsn = (os.environ.get("PROD_DB_URL") or "").strip().strip('"').strip("'")
    if not dsn or not ocd_db.is_prod_url(dsn):
        raise SystemExit("PROD_DB_URL ontbreekt of wordt niet als prod herkend.")
    return psycopg.connect(dsn, autocommit=False)


def omgevingsplan_work(gm: str) -> str:
    with lokale_conn() as c, c.cursor() as cur:
        cur.execute("""SELECT DISTINCT frbr_work FROM p2p.regeling
                        WHERE frbr_work LIKE %s AND documenttype = 'Omgevingsplan'
                          AND is_tijdelijkdeel_van IS NULL""", (f"/akn/nl/act/{gm}/%",))
        works = [r[0] for r in cur.fetchall()]
    if len(works) != 1:
        raise SystemExit(f"{gm}: verwacht één omgevingsplan in p2p.regeling, gevonden {works}")
    return works[0]


class Peiler:
    """Peilt de pons op een dag, met een cache zodat de bisectie geen dag twee keer vraagt."""

    def __init__(self, work: str):
        self.enc = a._encode_regeling_uri(work)
        self.nu = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cache: dict[date, tuple[str | None, str | None]] = {}
        self.calls = 0

    def __call__(self, d: date) -> tuple[str | None, str | None]:
        """(geometrieIdentificatie van de pons of None, expressionId of None)."""
        if d in self.cache:
            return self.cache[d]
        dag = d.isoformat()
        data = None
        for poging in range(6):
            self.calls += 1
            try:
                data = a._get(f"{a.cfg.PRESENTEREN_BASE}/regelingen/{self.enc}",
                              params={"_expand": "true", "locatieSelectie": "primair",
                                      "geldigOp": dag, "inWerkingOp": dag, "beschikbaarOp": self.nu})
                break
            except Exception as e:  # noqa: BLE001
                if "404" in str(e):  # het plan bestond toen nog niet
                    self.cache[d] = (None, None)
                    return self.cache[d]
                # Een peiling met datum geeft soms reeksen 503's waar een gewone call slaagt
                # (gezien 2026-11-12 's avonds). De retry van _get is daar te kort voor.
                if "503" not in str(e) or poging == 5:
                    raise
                time.sleep(15 * (poging + 1))
        pons = (data.get("_embedded") or {}).get("pons") or {}
        self.cache[d] = (pons.get("geometrieIdentificatie"), data.get("expressionId"))
        return self.cache[d]


def maanden(tot: date) -> list[date]:
    out, d = [], START
    while d <= tot:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    if out[-1] != tot:
        out.append(tot)
    return out


def stappen(peil: Peiler, vandaag: date) -> list[tuple[date, str | None, str | None]]:
    """De dagen waarop de pons verandert, met de nieuwe geometrieIdentificatie en expressie."""
    gevonden: dict[date, str | None] = {}

    def zoek(lo: date, hi: date) -> None:
        # lo en hi zijn gepeild en verschillen. Zoek elke overgang ertussen.
        if (hi - lo).days <= 1:
            gevonden[hi] = peil(hi)[0]
            return
        mid = lo + timedelta(days=(hi - lo).days // 2)
        g_lo, g_mid, g_hi = peil(lo)[0], peil(mid)[0], peil(hi)[0]
        if g_mid != g_lo:
            zoek(lo, mid)
        if g_mid != g_hi:
            zoek(mid, hi)

    punten = maanden(vandaag)
    gevonden[START] = peil(START)[0]
    for lo, hi in zip(punten, punten[1:]):
        if peil(lo)[0] != peil(hi)[0]:
            zoek(lo, hi)
    return [(d, g, peil(d)[1]) for d, g in sorted(gevonden.items())]


def geometrie(geom_id: str) -> str | None:
    gj = a._get_geometry(geom_id)
    if not gj:
        return None
    return json.dumps(gj.get("geometrie", gj) if isinstance(gj, dict) and "type" not in gj else gj)


def schrijf(conn: psycopg.Connection, gm: str, rijen: list[tuple], geoms: dict[str, str | None]) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL.read_text(encoding="utf-8"))
        cur.execute("DELETE FROM p2p.pons_historie WHERE gemeentecode = %s", (gm,))
        for d, gid, expr in rijen:
            gj = geoms.get(gid) if gid else None
            cur.execute("""
                INSERT INTO p2p.pons_historie (gemeentecode, geldig_vanaf, geometrie_identificatie,
                                               geometrie, expressie, gepeild_op)
                VALUES (%s, %s, %s,
                        CASE WHEN %s::text IS NULL THEN NULL
                             ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(
                                      ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 28992)), 3)) END,
                        %s, now())""", (gm, d, gid, gj, gj, expr))
    conn.commit()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gm", required=True, help="gemeentecodes, komma-gescheiden (gm0302,gm0392)")
    p.add_argument("--db", choices=["lokaal", "prod", "beide"], default="lokaal")
    p.add_argument("--droog", action="store_true", help="alleen peilen en tonen, niets schrijven")
    args = p.parse_args()

    gms = [g.strip().lower() for g in args.gm.split(",") if g.strip()]
    for gm in gms:
        if not RE_GM.match(gm):
            raise SystemExit(f"Geen gemeentecode: {gm}")

    vandaag = date.today()
    for gm in gms:
        work = omgevingsplan_work(gm)
        peil = Peiler(work)
        rijen = stappen(peil, vandaag)
        geoms: dict[str, str | None] = {}
        for _, gid, _ in rijen:
            if gid and gid not in geoms:
                geoms[gid] = geometrie(gid)
        print(f"{gm} {work}: {len(rijen)} stappen, {peil.calls} peilingen, {len(geoms)} geometrieën")
        for d, gid, expr in rijen:
            print(f"  {d}  {gid or '— geen pons'}  {expr or ''}"
                  + ("  (geometrie ontbreekt)" if gid and not geoms.get(gid) else ""))
        if args.droog:
            continue
        if args.db in ("lokaal", "beide"):
            with lokale_conn() as c:
                schrijf(c, gm, rijen, geoms)
            print("  geschreven: lokaal")
        if args.db in ("prod", "beide"):
            with prod_conn() as c:
                schrijf(c, gm, rijen, geoms)
            print("  geschreven: prod")


if __name__ == "__main__":
    main()
