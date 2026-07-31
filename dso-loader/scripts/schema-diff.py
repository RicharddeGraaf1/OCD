#!/usr/bin/env python
"""Pre-flight schemavergelijking tussen twee OCD-databases.

WAAROM dit bestaat
------------------
`full_sync.py` past bij elke run het schema toe uit `src/ddl.py`. Die kent
niet alles wat er in de database staat: verschillende objecten worden
aangemaakt door losse migratiescripts in deze map. Draai je een sync tegen
een doelwit waar die scripts nooit gedraaid hebben, dan ontbreken ze daar
stil — en applicaties die erop leunen falen zacht in plaats van hard.
`p2p.gio_referentie_consistentie_mv` is daar het voorbeeld van: de
annotatieconformiteit-scorer vangt een ontbrekende matview af met een
except-tak en rapporteert dan "niet getoetst" in plaats van een fout.

Dit script maakt dat gat zichtbaar VOORDAT je synchroniseert, niet erna.
Het is strikt read-only: alleen catalogus-queries, geen DDL, geen data.

Gebruik
-------
    python scripts/schema-diff.py                       # lokaal vs PROD_DB_URL
    python scripts/schema-diff.py --rechts <dsn>        # lokaal vs expliciet
    python scripts/schema-diff.py --links <dsn> --rechts <dsn>
    python scripts/schema-diff.py --schemas p2p,v2a --indexen

Exit-code 1 bij verschillen, zodat een sync-runbook erop kan gaten.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import cfg  # noqa: E402

# De schema's die de applicaties gebruiken. `audit` staat er bewust bij: dat
# draagt de sync-historie en loopt makkelijk uiteen.
STANDAARD_SCHEMAS = ["core", "p2p", "p2pwijziging", "wro", "i2a", "v2a", "conv", "audit"]

RELKIND = {
    "r": "tabel",
    "p": "tabel (partitioned)",
    "m": "matview",
    "v": "view",
    "f": "foreign table",
}


def masker(dsn: str) -> str:
    """Verberg gebruiker en wachtwoord in een connectstring."""
    return re.sub(r"://[^:/@]+(:[^@]+)?@", "://***@", dsn)


def lees_prod_dsn() -> str | None:
    from dotenv import dotenv_values

    waarden = dotenv_values(ROOT / ".env") or {}
    dsn = waarden.get("PROD_DB_URL") or os.getenv("PROD_DB_URL")
    return dsn.strip().strip('"').strip("'") if dsn else None


# ── Catalogus uitlezen ────────────────────────────────────────────────────

def haal_relaties(conn, schemas: list[str]) -> dict[tuple[str, str], str]:
    """{(schema, naam): soort} voor tabellen, views en matviews."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ns.nspname, cl.relname, cl.relkind
            FROM pg_class cl
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = ANY(%s) AND cl.relkind = ANY('{r,p,m,v,f}')
            """,
            (schemas,),
        )
        return {(s, n): RELKIND.get(k, k) for s, n, k in cur.fetchall()}


def haal_kolommen(conn, schemas: list[str]) -> dict[tuple[str, str], dict[str, str]]:
    """{(schema, tabel): {kolom: 'type NULL|NOT NULL'}}.

    Het type komt uit format_type, dus `character varying(200)` en niet het
    kale `varchar` — een lengteverschil is een echt verschil.
    """
    uit: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ns.nspname, cl.relname, at.attname,
                   format_type(at.atttypid, at.atttypmod), at.attnotnull
            FROM pg_attribute at
            JOIN pg_class cl ON cl.oid = at.attrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = ANY(%s)
              AND cl.relkind = ANY('{r,p,m,v,f}')
              AND at.attnum > 0 AND NOT at.attisdropped
            """,
            (schemas,),
        )
        for schema, tabel, kolom, typ, notnull in cur.fetchall():
            uit[(schema, tabel)][kolom] = f"{typ} {'NOT NULL' if notnull else 'NULL'}"
    return uit


def haal_indexen(conn, schemas: list[str]) -> dict[tuple[str, str], str]:
    """{(schema, indexnaam): genormaliseerde definitie}."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname, indexname, indexdef FROM pg_indexes WHERE schemaname = ANY(%s)",
            (schemas,),
        )
        # Normaliseer whitespace; de definitie zelf verschilt per Postgres-versie
        # in spaties, niet in betekenis.
        return {(s, n): re.sub(r"\s+", " ", d).strip() for s, n, d in cur.fetchall()}


# ── Vergelijken ───────────────────────────────────────────────────────────

def vergelijk(links: dict, rechts: dict, naam_links: str, naam_rechts: str, label: str) -> int:
    """Print de verschillen; geeft het aantal verschillen terug."""
    alleen_links = sorted(set(links) - set(rechts))
    alleen_rechts = sorted(set(rechts) - set(links))
    anders = sorted(k for k in set(links) & set(rechts) if links[k] != rechts[k])

    n = len(alleen_links) + len(alleen_rechts) + len(anders)
    if n == 0:
        print(f"  {label}: gelijk")
        return 0

    print(f"  {label}: {n} verschil(len)")
    for k in alleen_links:
        print(f"    alleen in {naam_links:<7} {'.'.join(k)}  ({links[k]})")
    for k in alleen_rechts:
        print(f"    alleen in {naam_rechts:<7} {'.'.join(k)}  ({rechts[k]})")
    for k in anders:
        print(f"    verschilt          {'.'.join(k)}")
        print(f"        {naam_links}:  {links[k]}")
        print(f"        {naam_rechts}: {rechts[k]}")
    return n


def vergelijk_kolommen(links: dict, rechts: dict, naam_links: str, naam_rechts: str) -> int:
    """Kolomverschillen, maar alleen voor relaties die aan beide kanten bestaan.

    Ontbreekt de hele tabel, dan is dat al gemeld bij de relatievergelijking;
    die dan ook nog kolom-voor-kolom uitspugen levert alleen ruis op.
    """
    gedeeld = sorted(set(links) & set(rechts))
    totaal = 0
    for sleutel in gedeeld:
        l, r = links[sleutel], rechts[sleutel]
        alleen_l = sorted(set(l) - set(r))
        alleen_r = sorted(set(r) - set(l))
        anders = sorted(k for k in set(l) & set(r) if l[k] != r[k])
        if not (alleen_l or alleen_r or anders):
            continue
        totaal += len(alleen_l) + len(alleen_r) + len(anders)
        print(f"    {'.'.join(sleutel)}")
        for k in alleen_l:
            print(f"      kolom alleen in {naam_links:<7} {k}  ({l[k]})")
        for k in alleen_r:
            print(f"      kolom alleen in {naam_rechts:<7} {k}  ({r[k]})")
        for k in anders:
            print(f"      kolom verschilt          {k}: {naam_links}={l[k]!r} {naam_rechts}={r[k]!r}")
    print(f"  kolommen: {totaal or 'geen'} verschil(len) over {len(gedeeld)} gedeelde relaties")
    return totaal


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--links", help="DSN links (default: lokale cfg.db_url)")
    p.add_argument("--rechts", help="DSN rechts (default: PROD_DB_URL uit .env)")
    p.add_argument("--naam-links", default="links")
    p.add_argument("--naam-rechts", default="rechts")
    p.add_argument("--schemas", help="komma-gescheiden (default: alle applicatieschema's)")
    p.add_argument("--indexen", action="store_true", help="ook indexen vergelijken (uitgebreid)")
    args = p.parse_args()

    dsn_links = args.links or cfg.db_url
    dsn_rechts = args.rechts or lees_prod_dsn()
    if not dsn_rechts:
        print("Geen doelwit: geef --rechts of zet PROD_DB_URL in dso-loader/.env.", file=sys.stderr)
        return 2

    schemas = [s.strip() for s in args.schemas.split(",")] if args.schemas else STANDAARD_SCHEMAS
    nl, nr = args.naam_links, args.naam_rechts

    print(f"{nl:<7} {masker(dsn_links)}")
    print(f"{nr:<7} {masker(dsn_rechts)}")
    print(f"schema's: {', '.join(schemas)}\n")

    with psycopg.connect(dsn_links) as cl, psycopg.connect(dsn_rechts) as cr:
        # Read-only: geen enkele query hieronder schrijft.
        for conn in (cl, cr):
            conn.read_only = True

        aanwezig_l = {s for s, _ in haal_relaties(cl, schemas)}
        aanwezig_r = {s for s, _ in haal_relaties(cr, schemas)}
        for s in schemas:
            if s not in aanwezig_l and s not in aanwezig_r:
                print(f"  let op: schema '{s}' bestaat aan geen van beide kanten")
            elif s not in aanwezig_l:
                print(f"  let op: schema '{s}' ontbreekt in {nl}")
            elif s not in aanwezig_r:
                print(f"  let op: schema '{s}' ontbreekt in {nr}")

        verschillen = vergelijk(
            haal_relaties(cl, schemas), haal_relaties(cr, schemas), nl, nr, "relaties"
        )
        verschillen += vergelijk_kolommen(
            haal_kolommen(cl, schemas), haal_kolommen(cr, schemas), nl, nr
        )
        if args.indexen:
            verschillen += vergelijk(
                haal_indexen(cl, schemas), haal_indexen(cr, schemas), nl, nr, "indexen"
            )

    print()
    if verschillen:
        print(f"{verschillen} verschil(len). Draai de ontbrekende migraties uit "
              f"scripts/ vóór de sync.")
        return 1
    print("Schema's zijn gelijk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
