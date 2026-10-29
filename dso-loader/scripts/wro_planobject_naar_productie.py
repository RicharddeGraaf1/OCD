# -*- coding: utf-8 -*-
"""Zet Wro-plannen en hun planobjecten over van lokaal naar productie (runbook stap 6f).

Tot 2026-10-29 bestond hiervoor geen route: `wro.planobject` kwam alleen via de
volledige restore op prod. Elke Wro-lading daarna bleef lokaal.

Wat telt als verschil, per plan (`ruimtelijk_instrument.idn`):
  nieuw       lokaal wel, prod niet
  gewijzigd   plan-attributen verschillen (hash zonder `laatst_geladen`), of het aantal
              planobjecten verschilt; met --inhoud ook de planobjecten zelf
              (hash per plan over alle kolommen, incl. geometrie)
  verdwenen   prod wel, lokaal niet — alleen gemeld, weg pas met --verwijder-verdwenen

Een plan-idn verandert per versie (IMRO), dus nieuw is meestal nieuw. --inhoud is voor
na een loaderfix die bestaande plannen herschreef: hij leest dan beide planobject-tabellen
volledig (lokaal 16 GB, prod 14 GB).

**Waarom geen DELETE op het plan.** `ruimtelijk_instrument` cascadeert naar
`wro_tekst_object` (de oude teksten, waar de API op terugvalt voor plannen zonder v2),
`wro_bronbestand` en `wro_geleideformulier`. Een plan dat blijft bestaan wordt daarom
ge-upsert; alleen zijn planobjecten gaan eruit en er opnieuw in. Eén transactie per plan:
een afgebroken run laat geen half plan achter.

Daarna, op prod: ANALYZE, een `core.load_run`-rij (die bumpt de tegel-ETag voor z11+),
en de generalisatie herbouwen voor z0-z10 (`vul_locatie_generalisatie.py --bron wro`).

    python scripts/wro_planobject_naar_productie.py                       # droogloop
    python scripts/wro_planobject_naar_productie.py --ja
    python scripts/wro_planobject_naar_productie.py --inhoud              # ook inhoud vergelijken
    python scripts/wro_planobject_naar_productie.py --idn NL.IMRO.0344.X --forceer --ja
"""
import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

HIER = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HIER, "..", ".env"))
LOKAAL = os.environ.get("DSO_DB") or "postgresql://postgres:postgres@localhost:5434/dso"
PROD = os.environ["PROD_DB_URL"].strip().strip('"')

# Kleine tabellen waar de plannen naar verwijzen. Volledig vergelijken is goedkoop;
# ontbrekende rijen gaan mee vóór de plannen, anders weigert de FK.
CODELIJSTEN = ["core.planstatus", "core.dossierstatus", "core.bestemmingshoofdgroep",
               "core.bouwaanduidingtype", "core.figuurtype", "core.gebiedsaanduidinghoofdgroep"]

# Tabellen zonder FK op het plan, die bij een verdwenen plan anders blijven hangen.
# De API leest een plan uit v2 zodra het in wro_tekst_laadstand staat.
ZONDER_FK = ["wro.wro_tekst_object_v2", "wro.wro_tekst_link", "wro.wro_tekst_laadstand",
             "v2a.wro_artikel_indeling"]


def kolommen(cur, tabel: str) -> list[str]:
    s, n = tabel.split(".")
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (s, n))
    return [r[0] for r in cur.fetchall()]


def als_tekst(kol: list[str]) -> str:
    # Alles als tekst over de lijn: COPY in tekstformaat leest geometrie (hex-EWKB),
    # jsonb en datums dan zonder adapters terug.
    return ", ".join(f"{k}::text" for k in kol)


def plan_stand(cur, ri_kol: list[str], inhoud: bool, po_kol: list[str],
               idns: list[str] | None = None) -> dict[str, tuple]:
    """idn -> (attribuut-hash, aantal planobjecten[, inhoud-hash]). Met idns: alleen die."""
    ri_waar, po_waar, arg = "", "", ()
    if idns is not None:
        ri_waar, po_waar, arg = "WHERE ri.idn = ANY(%s)", "WHERE instrument_idn = ANY(%s)", (idns,)
    attr = [k for k in ri_kol if k != "laatst_geladen"]
    expr = " || '|' || ".join(
        f"coalesce(md5(ST_AsEWKB({k})), '')" if k == "geometrie" else f"coalesce({k}::text, '')"
        for k in attr)
    cur.execute(f"""SELECT ri.idn, md5({expr}) AS h, coalesce(po.n, 0)
                      FROM wro.ruimtelijk_instrument ri
                      LEFT JOIN (SELECT instrument_idn, count(*) n FROM wro.planobject {po_waar}
                                  GROUP BY 1) po ON po.instrument_idn = ri.idn
                      {ri_waar}""", arg * 2)
    stand = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    if inhoud:
        rij = " || '|' || ".join(
            f"coalesce(md5(ST_AsEWKB({k})), '')" if k == "geometrie" else f"coalesce({k}::text, '')"
            for k in po_kol)
        cur.execute(f"""SELECT instrument_idn, md5(string_agg(md5({rij}), '' ORDER BY identificatie))
                          FROM wro.planobject {po_waar} GROUP BY 1""", arg)
        for idn, h in cur.fetchall():
            if idn in stand:
                stand[idn] = stand[idn] + (h,)
    return stand


def zet_codelijsten_over(l, p) -> int:
    n = 0
    for t in CODELIJSTEN:
        kol = kolommen(l, t)
        l.execute(f"SELECT {als_tekst(kol)} FROM {t}")
        rijen = l.fetchall()
        p.execute(f"CREATE TEMP TABLE _c (LIKE {t})")
        with p.copy(f"COPY _c ({', '.join(kol)}) FROM STDIN") as cp:
            for r in rijen:
                cp.write_row(r)
        p.execute(f"INSERT INTO {t} SELECT * FROM _c ON CONFLICT DO NOTHING")
        n += p.rowcount
        p.execute("DROP TABLE _c")
    return n


def zet_dossiers_over(l, p, idns: list[str]) -> int:
    """Manifest en dossier van deze plannen; dossier-status kan wijzigen, dus upsert."""
    l.execute("""SELECT DISTINCT m.overheidscode, m.naam_overheid, m.datum::text
                   FROM wro.ruimtelijk_instrument ri
                   JOIN wro.wro_dossier d ON d.dossiernummer = ri.dossier
                   JOIN wro.wro_manifest m ON m.overheidscode = d.manifest_code
                  WHERE ri.idn = ANY(%s)""", (idns,))
    for r in l.fetchall():
        p.execute("""INSERT INTO wro.wro_manifest (overheidscode, naam_overheid, datum)
                     VALUES (%s, %s, %s::date) ON CONFLICT (overheidscode) DO NOTHING""", r)
    l.execute("""SELECT DISTINCT d.dossiernummer, d.manifest_code, d.status
                   FROM wro.ruimtelijk_instrument ri
                   JOIN wro.wro_dossier d ON d.dossiernummer = ri.dossier
                  WHERE ri.idn = ANY(%s)""", (idns,))
    rijen = l.fetchall()
    for r in rijen:
        p.execute("""INSERT INTO wro.wro_dossier (dossiernummer, manifest_code, status)
                     VALUES (%s, %s, %s)
                     ON CONFLICT (dossiernummer) DO UPDATE SET status = EXCLUDED.status""", r)
    return len(rijen)


def zet_plan_over(l, p, idn: str, ri_kol: list[str], po_kol: list[str]) -> int:
    # Het plan via een tijdelijke tabel (ON COMMIT DELETE ROWS, zie main): COPY zet de
    # tekstwaarden om naar de kolomtypen, en daarna een gewone upsert.
    l.execute(f"SELECT {als_tekst(ri_kol)} FROM wro.ruimtelijk_instrument WHERE idn = %s", (idn,))
    with p.copy(f"COPY _plan ({', '.join(ri_kol)}) FROM STDIN") as cp:
        cp.write_row(l.fetchone())
    zet = ", ".join(f"{k} = EXCLUDED.{k}" for k in ri_kol if k != "idn")
    p.execute(f"""INSERT INTO wro.ruimtelijk_instrument ({', '.join(ri_kol)})
                  SELECT {', '.join(ri_kol)} FROM _plan
                  ON CONFLICT (idn) DO UPDATE SET {zet}""")
    p.execute("DELETE FROM wro.planobject WHERE instrument_idn = %s", (idn,))
    l.execute(f"SELECT {als_tekst(po_kol)} FROM wro.planobject WHERE instrument_idn = %s", (idn,))
    rijen = l.fetchall()
    if rijen:
        # Eén COPY per plan: de FK-controle op specificeert_id loopt aan het eind van
        # het statement, dus de volgorde binnen het plan doet er niet toe.
        with p.copy(f"COPY wro.planobject ({', '.join(po_kol)}) FROM STDIN") as cp:
            for r in rijen:
                cp.write_row(r)
    return len(rijen)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inhoud", action="store_true",
                    help="ook de planobjecten zelf vergelijken (leest beide tabellen volledig)")
    ap.add_argument("--idn", help="alleen deze plannen (komma-gescheiden)")
    ap.add_argument("--forceer", action="store_true",
                    help="met --idn: overzetten ook als er geen verschil is (om de route te toetsen)")
    ap.add_argument("--verwijder-verdwenen", action="store_true",
                    help="plannen die lokaal niet meer bestaan ook op prod verwijderen")
    ap.add_argument("--max-objecten", type=int, default=500_000,
                    help="weiger boven dit aantal over te zetten planobjecten (default 500.000)")
    ap.add_argument("--ja", action="store_true")
    a = ap.parse_args()
    if a.forceer and not a.idn:
        ap.error("--forceer alleen samen met --idn")

    # Prod in autocommit: dan opent elk `pc.transaction()`-blok een echte transactie die
    # aan het eind commit. Zonder autocommit loopt er na de eerste SELECT al een
    # transactie, worden de blokken savepoints, en commit pas alles bij het sluiten.
    with psycopg.connect(LOKAAL, autocommit=True) as lc, \
            psycopg.connect(PROD, connect_timeout=30, autocommit=True) as pc:
        l, p = lc.cursor(), pc.cursor()
        p.execute("SELECT current_database()")
        print(f"prod-database: {p.fetchone()[0]}", flush=True)

        ri_kol, po_kol = kolommen(l, "wro.ruimtelijk_instrument"), kolommen(l, "wro.planobject")
        for t, kol in (("wro.ruimtelijk_instrument", ri_kol), ("wro.planobject", po_kol)):
            if kolommen(p, t) != kol:
                sys.exit(f"STOP: kolommen van {t} verschillen tussen lokaal en prod — eerst het schema gelijk trekken.")

        t0 = time.time()
        alleen = a.idn.split(",") if a.idn else None
        lok = plan_stand(l, ri_kol, a.inhoud, po_kol, alleen)
        prd = plan_stand(p, ri_kol, a.inhoud, po_kol, alleen)
        print(f"vergeleken: {len(lok):,} plannen lokaal, {len(prd):,} op prod "
              f"({(time.time() - t0) / 60:.1f} min{', met inhoud' if a.inhoud else ''})", flush=True)

        if a.idn:
            gevraagd = set(a.idn.split(","))
            onbekend = gevraagd - lok.keys()
            if onbekend:
                sys.exit(f"STOP: lokaal onbekend: {', '.join(sorted(onbekend))}")
            nieuw = sorted(i for i in gevraagd if i not in prd)
            gewijzigd = sorted(i for i in gevraagd if i in prd and (a.forceer or lok[i] != prd[i]))
            verdwenen = []
        else:
            nieuw = sorted(lok.keys() - prd.keys())
            gewijzigd = sorted(i for i in lok.keys() & prd.keys() if lok[i] != prd[i])
            verdwenen = sorted(prd.keys() - lok.keys())

        over = nieuw + gewijzigd
        n_obj = sum(lok[i][1] for i in over)
        print(f"nieuw {len(nieuw):,} · gewijzigd {len(gewijzigd):,} · verdwenen {len(verdwenen):,} "
              f"· {n_obj:,} planobjecten over te zetten", flush=True)
        for soort, lijst in (("nieuw", nieuw), ("gewijzigd", gewijzigd), ("verdwenen", verdwenen)):
            for idn in lijst[:10]:
                print(f"  {soort:9s} {idn}")
            if len(lijst) > 10:
                print(f"  {soort:9s} … en nog {len(lijst) - 10:,}")

        if n_obj > a.max_objecten:
            sys.exit(f"STOP: {n_obj:,} planobjecten > --max-objecten {a.max_objecten:,}. "
                     f"Schijfruimte op apps-1 controleren en de grens bewust ophogen.")
        if not over and not (verdwenen and a.verwijder_verdwenen):
            print("niets te doen — prod is gelijk aan lokaal.")
            return
        if not a.ja:
            print("droogloop — niets geschreven. Draai met --ja.")
            return

        with pc.transaction():
            n_codes = zet_codelijsten_over(l, p)
            n_dos = zet_dossiers_over(l, p, over) if over else 0
        print(f"codelijsten: {n_codes} nieuwe codes · dossiers: {n_dos} bijgewerkt", flush=True)

        p.execute("CREATE TEMP TABLE _plan (LIKE wro.ruimtelijk_instrument) ON COMMIT DELETE ROWS")
        t0, totaal = time.time(), 0
        for i, idn in enumerate(over, 1):
            with pc.transaction():
                totaal += zet_plan_over(l, p, idn, ri_kol, po_kol)
            if i % 100 == 0 or i == len(over):
                print(f"  {i:,}/{len(over):,} plannen · {totaal:,} planobjecten · "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)

        if verdwenen and a.verwijder_verdwenen:
            with pc.transaction():
                for t in ZONDER_FK:
                    p.execute(f"DELETE FROM {t} WHERE instrument_idn = ANY(%s)", (verdwenen,))
                # cascadeert naar planobject, wro_tekst_object, wro_bronbestand, wro_geleideformulier
                p.execute("DELETE FROM wro.ruimtelijk_instrument WHERE idn = ANY(%s)", (verdwenen,))
            print(f"verdwenen plannen verwijderd: {len(verdwenen):,}", flush=True)

        p.execute("ANALYZE wro.ruimtelijk_instrument")
        p.execute("ANALYZE wro.planobject")
        # Een load_run-rij bumpt de ETag van /v1/tiles (max(finished_at)): zonder die rij
        # houden tegels van vóór de overzet bij revalidatie hun 304.
        p.execute("""INSERT INTO core.load_run (bron, scope, status, finished_at, n_verwerkt)
                     VALUES ('pdok-bestemmingsplannen', %s, 'ok', now(), %s)""",
                  (f"naar-prod:{len(over)} plannen", totaal))
        print(f"klaar: {len(over):,} plannen, {totaal:,} planobjecten. "
              f"Nu de generalisatie op prod herbouwen (runbook stap 6f).")


if __name__ == "__main__":
    main()
