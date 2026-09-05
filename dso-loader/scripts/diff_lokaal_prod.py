#!/usr/bin/env python
"""Tel élke tabel aan beide kanten en meld alleen wat onverwacht verschilt.

Waarom dit bestaat
------------------
De sync van 2026-08-28 vond twee gaten die geen enkele bestaande controle zag:
`p2p.tekstdeel_hoofdlijn` stond op prod op **0** tegen 4.955 lokaal, en van de
38 ontbrekende gebiedsaanwijzingen ontbrak ook de locatie eronder. Beide waren
alleen zichtbaar door met de hand te tellen. Dit script maakt van dat handwerk
een stap.

Het meet niet of een fase gedraaid heeft — dat doet de regressiecheck — maar of
de twee databases hetzelfde bevatten. Dat is een andere vraag, en het is de vraag
waar prod stil onvolledig van wordt.

De verwachtingen
----------------
Sommige verschillen horen er te zijn. Die staan in `diff_verwachtingen.yml` met
een reden erbij, zodat de uitvoer leeg is als alles klopt. Een controle die altijd
ruis geeft, wordt niet gelezen — precies de reden dat "0 fouten" jarenlang valse
geruststelling gaf.

Verwachtingen zijn **richtingsgevoelig** en hebben een marge, zodat een gat dat
groeit alsnog opvalt. Wie een verwachting toevoegt zonder reden krijgt een
waarschuwing.

    python scripts/diff_lokaal_prod.py            # alleen de verschillen
    python scripts/diff_lokaal_prod.py --alles    # ook wat gelijk is
    python scripts/diff_lokaal_prod.py --json     # machineleesbaar

Exitcode 0 = geen onverwacht verschil, 1 = wel, 2 = kon niet meten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
import yaml
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
VERWACHTINGEN = Path(__file__).resolve().parent / "diff_verwachtingen.yml"

# Schema's die aan beide kanten gelijk horen te zijn. Bewust niet:
#   audit    — lokale run-historie; prod heeft een eigen, kortere reeks
#   vangnet  — lokaal veiligheidsnet van stap 10, bestaat alleen hier
#   conv/lev — werkschema's van conversie-experimenten, niet gerepliceerd
#   public   — postgis-restant
SCHEMAS = ["core", "p2p", "p2pwijziging", "v2a", "i2a", "irm", "mer", "vth",
           "wro", "skos"]


def lokale_dsn() -> str:
    return (f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")


def tabelnamen(cur, schemas: list[str]) -> list[str]:
    cur.execute("""
        SELECT table_schema, table_name FROM information_schema.tables
         WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s)
         ORDER BY table_schema, table_name""", (schemas,))
    return [f"{s}.{t}" for s, t in cur.fetchall()]


def tel_alles(dsn: str, schemas: list[str], label: str,
              stil: bool = False) -> dict[str, int]:
    """Exacte rijtelling per tabel.

    Exact, geen `reltuples`-schatting: het gat dat dit script moet vinden was
    4.955 tegen 0, en juist bij zulke tabellen is de schatting het slechtst —
    `v2a.tekst_embedding` stond in de statistieken op 0 terwijl er 1,65 miljoen
    rijen in zaten (vault G-133).

    Eén `UNION ALL` over honderd tabellen leek sneller maar is het niet: hij
    dwingt de hele reeks in één transactie en gaf op 92 GB geen antwoord binnen
    tien minuten. Per tabel tellen kost evenveel werk maar toont voortgang, en
    een tabel die omvalt houdt de rest niet op. De twee kanten draaien parallel,
    dus de wandklok is die van de traagste kant en niet de som.
    """
    uit: dict[str, int] = {}
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SET statement_timeout = '15min'")
        namen = tabelnamen(cur, schemas)
        for i, tabel in enumerate(namen, 1):
            try:
                cur.execute(f"SELECT count(*) FROM {tabel}")
                uit[tabel] = cur.fetchone()[0]
            except Exception as e:
                conn.rollback()
                print(f"  [{label}] {tabel}: telling mislukt ({str(e)[:60]})",
                      file=sys.stderr, flush=True)
            if not stil and i % 25 == 0:
                print(f"  [{label}] {i}/{len(namen)} tabellen geteld",
                      file=sys.stderr, flush=True)
    return uit



# ── Inhoudscontrole ───────────────────────────────────────────────────────
# Tellen is niet genoeg. Op 2026-09-05 bleken acht locaties dezelfde sleutel te
# dragen met een ANDERE geometrie (gm0392 zes, gm0376 twee), samen goed voor
# 4.493 subdiv-stukjes. Voor een telling zijn die tabellen gelijk. Oorzaak: de
# replicatie upsert alleen locaties die in scope vallen, dus een geometrie die
# wijzigt terwijl haar locatie buiten scope ligt, blijft op prod voor altijd de
# oude. Zie vault G-142.
#
# Waarom per bronhouder en niet per rij: 321.096 geometrieën aan beide kanten
# vergelijken is een kwartier en levert een lijst die niemand leest. Eén hash per
# bronhouder is 382 regels, en pas als er één afwijkt hoef je de rijen erbij te
# halen. Gemeten: ~40 s per kant.
#
# Bewust alleen `p2p.locatie.geometrie`: daar is drift aangetoond en daar werkt
# hij door in subdiv -> generalisatie -> tiles.py. Uitbreiden pas als deze
# controle zich bewezen heeft.
GEOM_HASH_SQL = r"""
    SELECT substring(identificatie from 'nl\.imow-([a-z0-9]+)\.') AS bh,
           count(*)                                                 AS n,
           md5(string_agg(md5(ST_AsBinary(geometrie)), '' ORDER BY identificatie)) AS h
      FROM p2p.locatie
     WHERE geometrie IS NOT NULL
     GROUP BY 1
"""


def geometrie_vingerafdruk(dsn: str, label: str, stil: bool) -> dict[str, tuple[int, str]]:
    """Eén hash per bronhouder over de locatiegeometrie."""
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '30min'")
        cur.execute(GEOM_HASH_SQL)
        uit = {r[0]: (r[1], r[2]) for r in cur.fetchall() if r[0]}
    if not stil:
        print(f"  [{label}] geometrie-vingerafdruk over {len(uit)} bronhouders")
    return uit


def vergelijk_geometrie(lok: dict, prod: dict) -> list[dict]:
    """Bronhouders waar de geometrie-inhoud verschilt bij gelijk aantal rijen.

    Een verschil in aantal is al zichtbaar in de tabeltelling; hier gaat het om
    wat die telling juist NIET ziet.
    """
    rijen = []
    for bh in sorted(set(lok) | set(prod)):
        l, p = lok.get(bh), prod.get(bh)
        if l is None or p is None:
            rijen.append({"bronhouder": bh, "soort": "alleen aan één kant",
                          "lokaal": l[0] if l else None, "prod": p[0] if p else None})
        elif l[1] != p[1]:
            rijen.append({"bronhouder": bh,
                          "soort": "zelfde aantal, ANDERE geometrie" if l[0] == p[0]
                                   else "ander aantal én andere inhoud",
                          "lokaal": l[0], "prod": p[0]})
    return rijen


def migratie_verschil(prod_dsn: str) -> list[str]:
    """Migraties die de ene kant wel kent en de andere niet.

    Prod miste op 2026-09-04 alle drie de indexen uit
    2026-09-add-generalisatie-prefix-index.sql en op 05-09 een kolom, en niets
    meldde dat. Sinds 05-09 is er `core.migratie`; deze controle leest hem.
    De 73 migraties van vóór dat ledger staan nergens geregistreerd en tellen
    hier dus niet mee — dat is bewust: onbekend is geen ontbrekend.
    """
    def lees(dsn):
        try:
            with psycopg.connect(dsn, connect_timeout=20) as c, c.cursor() as cur:
                cur.execute("""SELECT bestand FROM core.migratie""")
                return {(r["bestand"] if isinstance(r, dict) else r[0])
                        for r in cur.fetchall()}
        except Exception:
            return None            # tabel bestaat nog niet aan die kant

    lok, pr = lees(lokale_dsn()), lees(prod_dsn)
    if lok is None or pr is None:
        return []
    uit = [f"prod mist: {n}" for n in sorted(lok - pr)]
    uit += [f"lokaal mist: {n}" for n in sorted(pr - lok)]
    return uit


def laad_verwachtingen() -> dict:
    if not VERWACHTINGEN.exists():
        return {}
    with VERWACHTINGEN.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("verwacht", {}) or {}


def beoordeel(tabel: str, lok: int | None, prod: int | None, verwacht: dict):
    """→ (status, toelichting). status ∈ gelijk | verwacht | AFWIJKING | ONTBREEKT."""
    if lok is None or prod is None:
        # Een tabel die maar aan één kant bestaat kon aanvankelijk nooit
        # "verwacht" worden, waardoor het script na de eerste triage nog altijd
        # elf afwijkingen meldde -- precies de ruis waar de kop van dit bestand
        # voor waarschuwt. `alleen: lokaal` / `alleen: prod` in het
        # verwachtingenbestand dekt dat af, en meldt het alsnog als de tabel aan
        # de verkeerde kant opduikt.
        kant = "prod" if prod is None else "lokaal"
        aanwezig = "lokaal" if prod is None else "prod"
        v = verwacht.get(tabel) or {}
        if v.get("alleen") == aanwezig:
            return "verwacht", v.get("reden", "(geen reden opgegeven — vul aan)")
        if v.get("alleen"):
            return ("AFWIJKING",
                    f"verwacht alleen aan de {v['alleen']}-kant, maar staat nu "
                    f"alleen aan de {aanwezig}-kant")
        return "ONTBREEKT", f"tabel bestaat niet aan de {kant}-kant"
    verschil = lok - prod
    if verschil == 0:
        return "gelijk", ""
    v = verwacht.get(tabel)
    if not v:
        return "AFWIJKING", f"{verschil:+,} onverklaard"
    ondergrens, bovengrens = v.get("min", v.get("verschil")), v.get("max", v.get("verschil"))
    if ondergrens is None or bovengrens is None:
        return "AFWIJKING", "verwachting zonder min/max in diff_verwachtingen.yml"
    if ondergrens <= verschil <= bovengrens:
        return "verwacht", v.get("reden", "(geen reden opgegeven — vul aan)")
    return ("AFWIJKING",
            f"{verschil:+,} valt buiten de verwachte marge [{ondergrens:+,}, {bovengrens:+,}] — "
            f"{v.get('reden', '')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--alles", action="store_true", help="toon ook de gelijke tabellen")
    ap.add_argument("--json", action="store_true", help="machineleesbare uitvoer")
    ap.add_argument("--schema", action="append", help="beperk tot deze schema's")
    ap.add_argument("--geen-inhoud", action="store_true",
                    help="sla de geometrie-inhoudscontrole over (alleen tellen, ~40 s sneller)")
    a = ap.parse_args()

    prod_dsn = os.getenv("PROD_DB_URL")
    if not prod_dsn:
        print("PROD_DB_URL ontbreekt in .env", file=sys.stderr)
        return 2
    schemas = a.schema or SCHEMAS

    # Beide kanten tegelijk: het zijn twee losse servers en de traagste bepaalt
    # de wandklok. Sequentieel duurde dit ruim het dubbele.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_lok = pool.submit(tel_alles, lokale_dsn(), schemas, "lokaal", a.json)
            f_prod = pool.submit(tel_alles, prod_dsn, schemas, "prod", a.json)
            lokaal, prod = f_lok.result(), f_prod.result()
    except Exception as e:
        print(f"kon niet meten: {e}", file=sys.stderr)
        return 2

    # Inhoudscontrole naast de telling. Draait standaard mee: een telling die
    # klopt terwijl de inhoud verschilt is precies het geval dat we op 2026-09-05
    # gemist zouden hebben.
    geom_afw: list[dict] = []
    if not a.geen_inhoud:
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                g_lok = pool.submit(geometrie_vingerafdruk, lokale_dsn(), "lokaal", a.json)
                g_prod = pool.submit(geometrie_vingerafdruk, prod_dsn, "prod", a.json)
                geom_afw = vergelijk_geometrie(g_lok.result(), g_prod.result())
        except Exception as e:
            print(f"geometrie-controle mislukt: {e}", file=sys.stderr)
            geom_afw = [{"bronhouder": "?", "soort": f"controle mislukt: {e}",
                         "lokaal": None, "prod": None}]

    mig = migratie_verschil(prod_dsn)

    verwacht = laad_verwachtingen()
    rijen = []
    for tabel in sorted(set(lokaal) | set(prod)):
        lok, pr = lokaal.get(tabel), prod.get(tabel)
        status, toelichting = beoordeel(tabel, lok, pr, verwacht)
        rijen.append({"tabel": tabel, "lokaal": lok, "prod": pr,
                      "status": status, "toelichting": toelichting})

    afwijkend = [r for r in rijen if r["status"] in ("AFWIJKING", "ONTBREEKT")]

    if a.json:
        print(json.dumps({"tabellen": len(rijen), "afwijkend": len(afwijkend),
                          "geometrie_afwijkend": geom_afw,
                          "rijen": rijen if a.alles else afwijkend},
                         ensure_ascii=False, indent=2))
        return 1 if (afwijkend or geom_afw or mig) else 0

    toon = rijen if a.alles else [r for r in rijen if r["status"] != "gelijk"]
    if toon:
        print(f"{'tabel':<40} {'lokaal':>12} {'prod':>12}  status")
        print("-" * 92)
        for r in toon:
            lok = "—" if r["lokaal"] is None else f"{r['lokaal']:,}"
            pr = "—" if r["prod"] is None else f"{r['prod']:,}"
            print(f"{r['tabel']:<40} {lok:>12} {pr:>12}  {r['status']}")
            if r["toelichting"]:
                print(f"{'':<40} {'':>12} {'':>12}  ↳ {r['toelichting']}")
        print()

    if geom_afw:
        print(f"{'bronhouder':<14} {'lokaal':>10} {'prod':>10}  geometrie")
        print("-" * 62)
        for g in geom_afw:
            lok = "—" if g["lokaal"] is None else f"{g['lokaal']:,}"
            pr = "—" if g["prod"] is None else f"{g['prod']:,}"
            print(f"{g['bronhouder']:<14} {lok:>10} {pr:>10}  {g['soort']}")
        print()

    if mig:
        print("MIGRATIES die maar aan één kant geregistreerd staan:")
        for m in mig:
            print(f"  {m}")
        print()

    gelijk = sum(1 for r in rijen if r["status"] == "gelijk")
    verw = sum(1 for r in rijen if r["status"] == "verwacht")
    print(f"{len(rijen)} tabellen · {gelijk} gelijk · {verw} verwacht verschil · "
          f"{len(afwijkend)} AFWIJKEND")
    if not a.geen_inhoud:
        print(f"geometrie-inhoud: {len(geom_afw)} bronhouder(s) met een verschil "
              f"dat een telling niet ziet")
    if afwijkend:
        print("\nEen afwijking is niet automatisch een fout, maar wél iets om te verklaren.")
        print("Klopt hij en hoort hij er te zijn, zet hem dan met reden in "
              f"{VERWACHTINGEN.name}.")
    if geom_afw and not afwijkend:
        print()
        print("De tellingen kloppen, maar de INHOUD verschilt. Dat is de klasse "
              "die alleen een vingerafdruk vindt — zie vault G-142.")
    return 1 if (afwijkend or geom_afw) else 0


if __name__ == "__main__":
    raise SystemExit(main())
