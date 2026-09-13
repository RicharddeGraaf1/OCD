#!/usr/bin/env python
"""Vergelijk de STRUCTUUR van beide databases: kolommen, constraints, indexen.

Waarom dit bestaat
------------------
`diff_lokaal_prod.py` telt rijen. Daardoor kon `v2a.artikel_indeling` op
productie **volledig zonder foreign key** staan terwijl lokaal een gevalideerde
`ON DELETE CASCADE` hangt — maandenlang, met 72 spookrijen als gevolg en een
teller die elke sync opliep. Het register leest die tabel rechtstreeks en toonde
dus categorieën voor artikelen die niet meer bestonden.

Dat gat was met geen enkele bestaande controle te zien:

- de rij-diff telt rijen, geen constraints;
- `repliceer_p2p_naar_prod.py` heeft wél een kolomdrift-controle, maar alleen
  over de 28 tabellen die hij kopieert — dat is toeval en geen systeem;
- het `core.migratie`-ledger weet alleen wat er sinds september bewust is
  toegepast en kent de 77 oudere migraties niet.

Zie vault `gaps.md` G-150 en `docs/sync-2026-09-13-bevindingen.md` §13.

Wat het meet
------------
Per tabel in de gedeelde schema's, aan beide kanten:

1. **kolommen** — naam, type, nullability en of er een default staat;
2. **constraints** — primary key, unique, foreign key, check, mét hun definitie
   én of ze gevalideerd zijn (een `NOT VALID`-FK dwingt niets af voor bestaande
   rijen en is dus een ander ding dan een gewone);
3. **indexen** — de volledige `indexdef`.

De verwachtingen
----------------
Net als bij de rij-diff: verschillen die er horen te zijn staan met een reden in
`diff_schema_verwachtingen.yml`, zodat de uitvoer leeg is als alles klopt. Een
controle die ruis geeft wordt niet gelezen.

    python scripts/diff_schema_lokaal_prod.py            # alleen de verschillen
    python scripts/diff_schema_lokaal_prod.py --alles    # ook wat gelijk is
    python scripts/diff_schema_lokaal_prod.py --json     # machineleesbaar

Exitcode 0 = geen onverwacht verschil, 1 = wel, 2 = kon niet meten.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
VERWACHTINGEN = Path(__file__).resolve().parent / "diff_schema_verwachtingen.yml"

# Dezelfde schema's als de rij-diff, om dezelfde redenen.
SCHEMAS = ["core", "p2p", "p2pwijziging", "v2a", "i2a", "irm", "mer", "vth",
           "wro", "skos"]


def lokale_dsn() -> str:
    return (f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")


# ── Uitlezen ──────────────────────────────────────────────────────────────

# Alleen BASE TABLEs, net als de rij-diff. Views meedoen op kolomniveau geeft
# ruis in plaats van signaal: een view die aan één kant ontbreekt levert dan één
# regel per kolom op (bij de eerste run 99 kolomverschillen, waarvan het merendeel
# vier ontbrekende views). Views worden hieronder als eigen soort vergeleken, op
# naam — dát is de vraag die je wilt beantwoorden: bestaat hij aan beide kanten.
KOLOMMEN_SQL = """
    SELECT n.nspname || '.' || t.relname AS tabel,
           a.attname                     AS kolom,
           format_type(a.atttypid, a.atttypmod) AS type,
           NOT a.attnotnull              AS nullable,
           a.atthasdef                   AS heeft_default
      FROM pg_attribute a
      JOIN pg_class t ON t.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = ANY(%s) AND t.relkind = 'r'
       AND a.attnum > 0 AND NOT a.attisdropped
"""

# Views en materialized views: alleen bestaan-of-niet. De definitie meevergelijken
# is een volgende stap; eerst het geval dat nu voorkomt (hij staat er wel of niet).
VIEWS_SQL = """
    SELECT n.nspname || '.' || c.relname AS naam,
           CASE c.relkind WHEN 'v' THEN 'view' ELSE 'matview' END AS soort
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = ANY(%s) AND c.relkind IN ('v', 'm')
"""

# `convalidated` hoort erbij: een FK die als NOT VALID is toegevoegd dwingt
# niets af voor bestaande rijen. Twee databases met dezelfde constraint-definitie
# maar een andere validatiestatus zijn niet hetzelfde.
CONSTRAINTS_SQL = """
    SELECT n.nspname || '.' || t.relname AS tabel,
           c.conname,
           pg_get_constraintdef(c.oid)   AS definitie,
           c.convalidated
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = ANY(%s) AND t.relkind = 'r'
"""

INDEXEN_SQL = """
    SELECT schemaname || '.' || tablename AS tabel,
           indexname,
           indexdef
      FROM pg_indexes
     WHERE schemaname = ANY(%s)
"""



_INDEX_NAAM_RE = re.compile(r"^CREATE (UNIQUE )?INDEX \S+ ON ", re.IGNORECASE)


def _index_sleutel(definitie: str) -> str:
    """De indexdefinitie zonder zijn naam, zodat hernoemde indexen matchen."""
    return _INDEX_NAAM_RE.sub(lambda m: f"CREATE {m.group(1) or ''}INDEX ON ", definitie)


def lees_structuur(dsn: str, label: str) -> dict:
    """Haal kolommen, constraints en indexen op in één verbinding."""
    uit = {"kolom": {}, "constraint": {}, "index": {}, "view": {}}
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '5min'")

        cur.execute(KOLOMMEN_SQL, (SCHEMAS,))
        for tabel, kolom, typ, nullable, heeft_def in cur.fetchall():
            uit["kolom"][f"{tabel}.{kolom}"] = f"{typ} nullable={nullable} default={heeft_def}"

        cur.execute(VIEWS_SQL, (SCHEMAS,))
        for naam, soort in cur.fetchall():
            uit["view"][naam] = soort

        cur.execute(CONSTRAINTS_SQL, (SCHEMAS,))
        for tabel, naam, definitie, gevalideerd in cur.fetchall():
            # Sleutel op tabel + definitie, niet op de constraint-naam: een
            # automatisch gegenereerde naam kan aan beide kanten verschillen
            # terwijl de constraint identiek is. Wat telt is wat hij afdwingt.
            uit["constraint"][f"{tabel} :: {definitie}"] = (
                f"naam={naam} gevalideerd={gevalideerd}")

        cur.execute(INDEXEN_SQL, (SCHEMAS,))
        for tabel, naam, definitie in cur.fetchall():
            # Sleutel op WAT er geindexeerd wordt, niet op hoe de index heet.
            # De naam is geen betrouwbare sleutel: de schaduwtabel-swaps maken
            # hun indexen onder een eigen naam aan, zodat prod bijvoorbeeld
            # `ai_regeling_idx0` heeft waar lokaal `artikel_indeling_regeling_idx`
            # staat -- dezelfde index op dezelfde kolom. Op naam vergelijken gaf
            # daardoor 13 valse "prod mist deze index", en een controle met
            # valse meldingen wordt niet gelezen.
            uit["index"][_index_sleutel(definitie)] = naam
    print(f"  [{label}] {len(uit['kolom'])} kolommen · "
          f"{len(uit['constraint'])} constraints · {len(uit['index'])} indexen · "
          f"{len(uit['view'])} views",
          file=sys.stderr, flush=True)
    return uit


# ── Vergelijken ───────────────────────────────────────────────────────────

RIJ_VERWACHTINGEN = Path(__file__).resolve().parent / "diff_verwachtingen.yml"


def laad_verwachtingen() -> dict[str, str]:
    """Eigen verwachtingen, plus de tabellen die de rij-diff al accepteert.

    Een tabel die maar aan een kant bestaat is dezelfde constatering voor beide
    controles, en de reden staat al ergens opgeschreven. Die tweede keer
    overtypen zou hem laten verouderen op de plek waar niemand kijkt. Dus: staat
    een tabel in `diff_verwachtingen.yml`, dan is zijn eenzijdigheid hier ook
    verwacht, met dezelfde reden en een verwijzing erbij.

    Alleen voor `tabel:` -- een gedocumenteerd rijverschil zegt niets over een
    ontbrekende constraint of index binnen een tabel die aan beide kanten staat.
    """
    uit: dict[str, str] = {}
    if RIJ_VERWACHTINGEN.exists():
        rij = yaml.safe_load(RIJ_VERWACHTINGEN.read_text(encoding="utf-8")) or {}
        for tabel, spec in (rij.get("verwacht") or {}).items():
            reden = (spec or {}).get("reden", "").strip()
            if reden:
                uit[f"tabel:{tabel}"] = f"[uit diff_verwachtingen.yml] {reden}"

    if not VERWACHTINGEN.exists():
        return uit
    data = yaml.safe_load(VERWACHTINGEN.read_text(encoding="utf-8")) or {}
    verwacht = data.get("verwacht") or {}
    zonder_reden = [k for k, v in verwacht.items()
                    if not (v or {}).get("reden", "").strip()]
    for k in zonder_reden:
        print(f"  ! verwachting zonder reden: {k}", file=sys.stderr)
    uit.update({k: (v or {}).get("reden", "") for k, v in verwacht.items()})
    return uit


def eenzijdige_tabellen(lok: dict, prod: dict) -> dict[str, str]:
    """Tabellen die maar aan een kant bestaan, afgeleid uit de kolomlijst.

    Zonder deze oprolling levert een ontbrekende tabel een regel per kolom, per
    index en per constraint op. Bij de eerste run was dat het verschil tussen
    155 regels en een handvol: van de 78 kolomverschillen waren er 75 "hele
    tabel staat er niet", en slechts 3 echte kolomdrift binnen een gedeelde
    tabel. Die drie zijn het signaal; de rest is een feit dat 75 keer terugkomt.
    """
    def tabellen(kant):
        return {sleutel.rsplit(".", 1)[0] for sleutel in kant["kolom"]}
    L, P = tabellen(lok), tabellen(prod)
    uit = {t: "alleen lokaal" for t in L - P}
    uit.update({t: "alleen prod" for t in P - L})
    return uit


def vergelijk(lok: dict, prod: dict, verwacht: dict[str, str]) -> list[dict]:
    """Alle verschillen, elk met zijn soort en richting."""
    bevindingen = []
    eenzijdig = eenzijdige_tabellen(lok, prod)
    for tabel, richting in sorted(eenzijdig.items()):
        kant = lok if richting == "alleen lokaal" else prod
        n_kol = sum(1 for k in kant["kolom"] if k.rsplit(".", 1)[0] == tabel)
        bevindingen.append({
            "soort": "tabel", "sleutel": tabel, "richting": richting,
            "detail": f"{n_kol} kolommen; verschillen binnen deze tabel zijn "
                      f"hier niet apart gemeld",
            "reden": verwacht.get(f"tabel:{tabel}"),
        })

    def binnen_eenzijdige_tabel(soort, sleutel):
        if soort == "view":
            return False
        if soort == "constraint":
            tabel = sleutel.split(" :: ")[0]
        elif soort == "index":
            # de sleutel is nu de definitie: "... INDEX ON <schema>.<tabel> USING ..."
            m = re.search(r" ON ([a-z0-9_]+\.[a-z0-9_]+) ", sleutel)
            tabel = m.group(1) if m else ""
        else:
            tabel = sleutel.rsplit(".", 1)[0]
        return tabel in eenzijdig

    for soort in ("kolom", "constraint", "index", "view"):
        L, P = lok[soort], prod[soort]
        for sleutel in sorted(set(L) | set(P)):
            if binnen_eenzijdige_tabel(soort, sleutel):
                continue
            in_l, in_p = sleutel in L, sleutel in P
            if in_l and in_p:
                if L[sleutel] == P[sleutel]:
                    continue
                richting, detail = "verschilt", f"lokaal {L[sleutel]} · prod {P[sleutel]}"
            elif in_l:
                richting, detail = "alleen lokaal", L[sleutel]
            else:
                richting, detail = "alleen prod", P[sleutel]
            bevindingen.append({
                "soort": soort, "sleutel": sleutel,
                "richting": richting, "detail": detail,
                "reden": verwacht.get(f"{soort}:{sleutel}"),
            })
    return bevindingen


def ernst(b: dict) -> int:
    """Sorteervolgorde. Een ontbrekende constraint op prod staat bovenaan.

    Dat is niet willekeurig: dát was het geval dat dit script bestaansrecht gaf.
    Een constraint die lokaal wél en op prod niet bestaat, betekent dat prod
    iets toelaat wat de werkbank verbiedt — en dat is per definitie stille drift.
    """
    if b["reden"]:
        return 9
    if b["soort"] == "constraint" and b["richting"] == "alleen lokaal":
        return 0
    if b["soort"] == "constraint":
        return 1
    if b["soort"] in ("kolom", "tabel", "view"):
        return 2
    return 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alles", action="store_true", help="toon ook de verwachte verschillen")
    ap.add_argument("--json", action="store_true", help="machineleesbaar")
    args = ap.parse_args()

    prod_dsn = os.getenv("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_lok = pool.submit(lees_structuur, lokale_dsn(), "lokaal")
            f_prod = pool.submit(lees_structuur, prod_dsn, "prod")
            lok, prod = f_lok.result(), f_prod.result()
    except Exception as e:
        print(f"kon de structuur niet uitlezen: {e}", file=sys.stderr)
        return 2

    verwacht = laad_verwachtingen()
    bevindingen = sorted(vergelijk(lok, prod, verwacht),
                         key=lambda b: (ernst(b), b["soort"], b["sleutel"]))
    onverwacht = [b for b in bevindingen if not b["reden"]]

    if args.json:
        print(json.dumps({"bevindingen": bevindingen,
                          "onverwacht": len(onverwacht)}, ensure_ascii=False, indent=2))
        return 1 if onverwacht else 0

    toon = bevindingen if args.alles else onverwacht
    if toon:
        print()
        for b in toon:
            vlag = "verwacht " if b["reden"] else "AFWIJKING"
            print(f"{vlag}  [{b['soort']:10}] {b['richting']:13} {b['sleutel']}")
            print(f"                          {b['detail']}")
            if b["reden"]:
                print(f"                          ↳ {b['reden'].strip()}")

    totaal = {s: sum(1 for b in bevindingen if b["soort"] == s)
              for s in ("tabel", "kolom", "constraint", "index", "view")}
    print(f"\nverschillen: {totaal['tabel']} tabel · {totaal['kolom']} kolom "
          f"· {totaal['constraint']} constraint · {totaal['index']} index "
          f"· {totaal['view']} view · waarvan {len(onverwacht)} ONVERWACHT")
    if onverwacht:
        print("\nEen afwijking is niet automatisch een fout, maar wél iets om te verklaren.")
        print("Klopt hij en hoort hij er te zijn, zet hem dan met reden in")
        print("diff_schema_verwachtingen.yml — met de sleutel zoals hij hierboven staat,")
        print("voorafgegaan door de soort, bijvoorbeeld  index:p2p.locatie.idx_foo")
    return 1 if onverwacht else 0


if __name__ == "__main__":
    sys.exit(main())
