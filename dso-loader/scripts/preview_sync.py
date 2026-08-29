"""preview_sync.py — READ-ONLY: wat zou een sync gaan laden?

Draai dit vóór `full_sync.py`. Het schrijft **niets** (geen INSERT/UPDATE, geen
`load_run`, geen rapport) en bevraagt de bronnen alleen met lichte lijst-calls.
Ook aanroepbaar als `full_sync.py --preview`.

    python scripts/preview_sync.py                    # lokale DB
    python scripts/preview_sync.py --target prod      # tegen prod (read-only)
    python scripts/preview_sync.py --vergelijk-prod   # lokaal + prod naast elkaar
    python scripts/preview_sync.py --i2a              # ook de i2a-poll previewen
    python scripts/preview_sync.py --json             # machineleesbaar

Wat per bron wordt getoond:

| bron  | preview-kost              | wat je te zien krijgt                    |
|-------|---------------------------|------------------------------------------|
| p2p   | ~10 lijst-calls           | nieuw / nieuwe versie / verdwenen (G-91)  |
| vth   | 1 SRU-call per open dag   | aantal kennisgevingen per openstaande dag |
| i2a   | ~342 calls (opt-in)       | activiteiten in de RTR vs. in de DB       |
| embed | alleen DB                 | tekst_elementen zonder embedding          |

**Waarom dit bestaat**: de p2p-delta stopte tot 2026-08-01 bij het eerste item
ouder dan `sinds`, in de aanname dat `_sort=-registratietijdstip` een strikt
gesorteerde lijst gaf. Dat is niet zo — de sync miste daardoor 16 regelingen en
rapporteerde "0 fouten". Een sync die zegt wat hij gaat doen vóórdat hij het
doet, maakt die klasse fouten zichtbaar. Zie `gaps.md` G-98 en
`docs/synchronisatieproces_beschrijving.md`.
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _masker_dsn(dsn: str) -> str:
    return re.sub(r"://[^:/@]+(:[^@]+)?@", "://***@", dsn)


def kies_doelwit_db(target: str, dsn: str | None) -> str:
    """Zet OCD_DB_URL vóór de eerste DB-connectie. Read-only: geen bevestiging."""
    if not dsn and target == "prod":
        from dotenv import dotenv_values
        dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if not dsn:
            raise SystemExit("PROD_DB_URL ontbreekt in .env — kan niet tegen prod previewen.")
    if not dsn:
        return "LOKAAL"
    dsn = dsn.strip().strip('"').strip("'")
    os.environ["OCD_DB_URL"] = dsn
    return f"PROD ({_masker_dsn(dsn)})" if target == "prod" else _masker_dsn(dsn)


# ── p2p ──────────────────────────────────────────────────────────────

def preview_p2p(sinds: str | None, vergelijk_prod: bool = False) -> dict:
    """Lijst-inventarisatie DSO vs. DB: nieuw, nieuwe versie, verdwenen.

    `sinds=None` = de volledige lijst bekijken (aanbevolen voor de preview: de
    kost is gelijk, en je ziet ook achterstand van vóór de watermark).
    """
    from src.db import get_conn
    from src.loaders.api_loader import find_regelingen_delta
    from full_sync import bouw_bronhouderlijst

    codes = {bh.overheid_code for bh in bouw_bronhouderlijst()}
    regs = find_regelingen_delta(sinds, bronhouder_codes=codes)

    exprs = [r.get("expressionId") or r["identificatie"] for r in regs]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT regeling_expression e FROM p2p.tekst_element "
                "WHERE regeling_expression = ANY(%s)", (exprs,))
    geladen = {r["e"] for r in cur.fetchall()}
    cur.execute("SELECT frbr_work w FROM p2p.regeling")
    works = {r["w"] for r in cur.fetchall()}

    nieuw, nieuwe_versie = [], []
    for r in regs:
        e = r.get("expressionId") or r["identificatie"]
        if e in geladen:
            continue
        (nieuwe_versie if r["identificatie"] in works else nieuw).append(r)

    # Omgekeerde diff (G-91): vigerend in de DB, niet meer in de DSO-lijst.
    dso_exprs = {r.get("expressionId") or r["identificatie"] for r in regs}
    dso_works = {r["identificatie"] for r in regs}
    cur.execute("SELECT frbr_expression e, frbr_work w, opschrift o "
                "FROM p2p.regeling WHERE NOT inactief")
    verdwenen, verdrongen = [], []
    for r in cur.fetchall():
        if r["e"] in dso_exprs:
            continue
        # Staat het work er nog wél in? Dan is dit een verdrongen oude versie
        # (op te ruimen via markeer_verouderde_expressies.py), geen intrekking.
        (verdrongen if r["w"] in dso_works else verdwenen).append(r)

    prod_mist = None
    if vergelijk_prod:
        import psycopg
        from dotenv import dotenv_values
        from psycopg.rows import dict_row
        prod_dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if prod_dsn:
            with psycopg.connect(prod_dsn, row_factory=dict_row, connect_timeout=20) as p:
                pc = p.cursor()
                pc.execute("SELECT DISTINCT regeling_expression e FROM p2p.tekst_element "
                           "WHERE regeling_expression = ANY(%s)", (exprs,))
                prod_geladen = {r["e"] for r in pc.fetchall()}
            prod_mist = [r for r in regs
                         if (r.get("expressionId") or r["identificatie"]) not in prod_geladen]
    conn.close()

    return {"in_dso": len(regs), "nieuw": nieuw, "nieuwe_versie": nieuwe_versie,
            "verdrongen": verdrongen, "verdwenen": verdwenen, "prod_mist": prod_mist}


def print_p2p(p: dict):
    print(f"\n{'═' * 100}\np2p — Ow-regelingen (Presenteren v8)\n{'═' * 100}")
    print(f"DSO-lijst binnen scope: {p['in_dso']}")
    print(f"  TE LADEN: {len(p['nieuw'])} nieuw + {len(p['nieuwe_versie'])} nieuwe versie "
          f"= {len(p['nieuw']) + len(p['nieuwe_versie'])}")
    if p["prod_mist"] is not None:
        print(f"  (prod mist er {len(p['prod_mist'])})")
    for kop, items in (("NIEUW", p["nieuw"]), ("NIEUWE VERSIE", p["nieuwe_versie"])):
        if not items:
            continue
        print(f"\n  {kop}:")
        for r in sorted(items, key=lambda r: r["tijdstipRegistratie"], reverse=True):
            print(f"    {r['tijdstipRegistratie'][:10]}  {r['bronhouder_code']:9} "
                  f"{str(r['type'])[:24]:26} {str(r['titel'] or '')[:44]}")
    if p["nieuw"] or p["nieuwe_versie"]:
        types = Counter(str(r["type"]) for r in p["nieuw"] + p["nieuwe_versie"])
        print(f"\n  per documenttype: {dict(types)}")
    if p["verdrongen"]:
        print(f"\n  VERDRONGEN (oude versie nog vigerend in de DB; na het laden "
              f"markeer_verouderde_expressies.py): {len(p['verdrongen'])}")
        for r in p["verdrongen"][:10]:
            print(f"    {str(r['o'])[:60]}")
    if p["verdwenen"]:
        print(f"\n  VERDWENEN uit de DSO-lijst (G-91 — sync ruimt dit NIET op): "
              f"{len(p['verdwenen'])}")
        for r in p["verdwenen"][:10]:
            print(f"    {str(r['o'])[:60]}")


# ── vth ──────────────────────────────────────────────────────────────

def preview_vth() -> dict:
    """Welke dagen staan open, en hoeveel kennisgevingen zitten daarin?"""
    from src.db import get_conn
    from src.loaders.koop_vergunning import fetch_page

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT max(processed_date)::text d FROM vth.etl_run WHERE status='ok'")
    laatste = (cur.fetchone() or {}).get("d")
    cur.execute("SELECT count(*) n, max(datum_publicatie)::text d FROM vth.vergunningkennisgeving")
    stand = cur.fetchone()
    cur.execute("SELECT count(*) n FROM vth.vergunningkennisgeving WHERE inhoud_geladen_at IS NULL")
    te_verrijken = cur.fetchone()["n"]
    conn.close()

    vandaag = datetime.date.today()
    vanaf = (datetime.date.fromisoformat(laatste) + datetime.timedelta(days=1)
             if laatste else vandaag - datetime.timedelta(days=7))
    dagen = []
    d = vanaf
    while d <= vandaag:
        query = (f'dt.type="omgevingsvergunning" AND dt.modified>={d.isoformat()} '
                 f'AND dt.modified<{(d + datetime.timedelta(days=1)).isoformat()}')
        try:
            _, totaal = fetch_page(query, 1, 1)
        except Exception as e:
            totaal = f"?? ({type(e).__name__})"
        dagen.append((d.isoformat(), totaal))
        d += datetime.timedelta(days=1)
    return {"laatste_dag": laatste, "stand": stand, "dagen": dagen,
            "te_verrijken": te_verrijken}


def print_vth(v: dict):
    print(f"\n{'═' * 100}\nvth — vergunningkennisgevingen (KOOP SRU)\n{'═' * 100}")
    print(f"in de DB: {v['stand']['n']} records, laatste publicatie {v['stand']['d']}; "
          f"laatst verwerkte dag {v['laatste_dag']}")
    tot = sum(n for _, n in v["dagen"] if isinstance(n, int))
    print(f"  TE LADEN: {len(v['dagen'])} open dag(en), samen {tot} kennisgevingen")
    for dag, n in v["dagen"]:
        print(f"    {dag}: {n}")
    print(f"  nog te verrijken (inhoud_geladen_at IS NULL): {v['te_verrijken']} "
          f"(~{v['te_verrijken'] / 4 / 60:.0f} min bij 4/s)")


# ── i2a ──────────────────────────────────────────────────────────────

def preview_i2a() -> dict:
    """RTR-activiteiten per gemeente (1 lichte call each) vs. wat in de DB zit.

    Gebruikt bewust dezelfde twee helpers als de loader — `_rtr_organisatiecode`
    en `_peildatum` — zodat de preview laat zien wat de loader zou zien.

    Beide waren hier tot 2026-08-09 fout overgeschreven in plaats van
    hergebruikt: de preview stuurde `gm0344` (de RTR wil `0344`, zie G-117) en
    de hardgecodeerde april-datum. De eerste maakte dat élke gemeente 0
    activiteiten leek te hebben — dezelfde stille nul als in de loader zelf.

    LET OP: dit telt `page.totalElements` bij `pageSize=1`, en dat veld ligt in
    de RTR structureel lager dan het werkelijke aantal items (Amsterdam: 120
    items, veld zegt 113; gm1699: 100 om 90). Voor een preview — is er iets, en
    grofweg hoeveel — volstaat dat; als exacte telling niet.
    """
    from src.db import get_conn
    from src.loaders.imtr_loader import _api_post, _peildatum, _rtr_organisatiecode
    from src.config import cfg
    from src.pipeline.bronhouders import filter_by_type
    from full_sync import bouw_bronhouderlijst

    gemeenten = filter_by_type(bouw_bronhouderlijst(), "gemeente")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT count(*) n FROM i2a.regelbeheerobject")
    in_db = cur.fetchone()["n"]
    conn.close()

    per_gem, fouten = {}, []
    for bh in gemeenten:
        try:
            data = _api_post(cfg.RTR_BASE, "/activiteiten/_zoek", {
                "datum": _peildatum(),
                "bestuursorgaan": {
                    "organisatieCode": _rtr_organisatiecode(bh.overheid_code)},
                "pageSize": 1, "page": 1})
            per_gem[bh.overheid_code] = data.get("page", {}).get("totalElements", 0)
        except Exception as e:
            fouten.append(f"{bh.overheid_code}: {type(e).__name__}")
    return {"gemeenten": len(gemeenten), "peildatum": _peildatum(),
            "activiteiten_rtr": sum(per_gem.values()),
            "regelbeheerobject_in_db": in_db, "zonder_activiteiten":
            sum(1 for v in per_gem.values() if not v), "fouten": fouten}


def print_i2a(i: dict):
    print(f"\n{'═' * 100}\ni2a — toepasbare regels (RTR/STTR)\n{'═' * 100}")
    print(f"  {i['gemeenten']} gemeenten gepolld; {i['activiteiten_rtr']} activiteiten in de RTR "
          f"({i['zonder_activiteiten']} gemeenten zonder)")
    print(f"  regelbeheerobjecten in de DB: {i['regelbeheerobject_in_db']}")
    print("  NB: de i2a-delta zit op de DMN-download, niet op de lijst — de fase pollt")
    print("      altijd alle gemeenten; 'te laden' is dus niet exact vooraf te bepalen.")
    print(f"  peildatum: {i['peildatum']}")
    if i["fouten"]:
        print(f"  fouten bij het pollen: {len(i['fouten'])} — {', '.join(i['fouten'][:5])}")


# ── embeddings ───────────────────────────────────────────────────────

def preview_embed() -> dict:
    from src.db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT count(*) n FROM p2p.tekst_element te
                   WHERE NOT EXISTS (SELECT 1 FROM v2a.tekst_embedding e
                                     WHERE e.tekst_element_id = te.id)""")
    zonder = cur.fetchone()["n"]
    cur.execute("SELECT count(*) n FROM v2a.tekst_embedding")
    totaal = cur.fetchone()["n"]
    conn.close()
    return {"embeddings": totaal, "tekst_element_zonder_embedding": zonder}


def print_embed(e: dict):
    print(f"\n{'═' * 100}\nembed — vectorindex (v2a)\n{'═' * 100}")
    print(f"  embeddings in de DB: {e['embeddings']}")
    print(f"  TE EMBEDDEN: {e['tekst_element_zonder_embedding']} tekst_elementen zonder embedding")
    print("  NB: de embed-fase herbouwt chunk_annotatie/chunk_categorie volledig (uren),")
    print("      ook als er weinig nieuw is — zie gaps G-97.")


# ── main ─────────────────────────────────────────────────────────────

def main_vanuit_sync(sync_args):
    """Entry-point voor `full_sync.py --preview`.

    Neemt de skip-vlaggen van de sync over, zodat de preview precies de fasen
    toont die die run ook zou draaien. De doelwit-DB is dan al gekozen door
    `full_sync.kies_doelwit_db`, dus die zetten we hier niet opnieuw.
    """
    print(f"PREVIEW (read-only) — {datetime.date.today()}")
    if not getattr(sync_args, "skip_p2p", False):
        # sinds=None: de volledige lijst kost evenveel calls als een delta en
        # toont ook achterstand van vóór de watermark (zie G-98).
        print_p2p(preview_p2p(sync_args.sinds))
    if not getattr(sync_args, "skip_vth", False):
        print_vth(preview_vth())
    if not getattr(sync_args, "skip_i2a", False):
        print(f"\n{'═' * 100}\ni2a — toepasbare regels (RTR/STTR)\n{'═' * 100}")
        print("  niet gepreviewd (kost ~342 calls) — draai "
              "`python scripts/preview_sync.py --i2a` als je dit wilt zien.")
    if not getattr(sync_args, "skip_embed", False):
        print_embed(preview_embed())
    print(f"\n{'═' * 100}\nNiets geladen — dit was een preview. Laat --preview weg "
          f"om het echt te doen.\n{'═' * 100}")


def docker_preflight() -> None:
    """Start Docker Desktop als de engine niet draait, en wijs anders WSL aan.

    Twee syncs op rij (2026-08-21 en 2026-08-28) begonnen met een dode engine, en
    beide keren kostte het de eerste tien minuten van de run. De symptomen
    verschillen: op 21-08 draaiden de Docker-processen wél maar stond de
    WSL-distro `docker-desktop` op `Stopped`; op 28-08 draaide er niets.

    De verwarrende variant is de eerste. `docker ps` hangt dan zonder foutmelding
    en dat lijkt een Docker-probleem, terwijl `wsl --list --verbose` het in één
    regel aanwijst. Vandaar dat we die uitvoer tonen in plaats van alleen te
    melden dat het misging.

    Bijvangst die de moeite van het weten waard is: een onreine afsluiting van de
    container gooit de cumulatieve statistieken van PostgreSQL weg (vault G-133),
    dus een dode engine is niet alleen vertraging bij de start maar ook een trage
    database daarna. full_sync.py vangt dat op in zijn eigen preflight.
    """
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("docker"):
        return
    def _engine_ok() -> bool:
        try:
            return _sp.run(["docker", "ps"], capture_output=True, timeout=25).returncode == 0
        except Exception:
            return False
    if _engine_ok():
        return
    print("Docker-engine reageert niet — Docker Desktop starten...", flush=True)
    for pad in (r"C:\Program Files\Docker\Docker\Docker Desktop.exe",):
        if Path(pad).exists():
            try:
                _sp.Popen([pad])
            except Exception as e:
                print(f"  starten mislukt: {e}")
            break
    else:
        print("  Docker Desktop niet op de verwachte plek gevonden.")
    for _ in range(30):          # tot ~2,5 minuut
        time.sleep(5)
        if _engine_ok():
            print("  engine is op.", flush=True)
            return
    print("  engine komt niet op. Kijk eerst naar WSL, niet naar Docker:", flush=True)
    try:
        r = _sp.run(["wsl", "--list", "--verbose"], capture_output=True, timeout=20)
        print((r.stdout or b"").decode("utf-16-le", "replace").replace(chr(0), ""))
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="READ-ONLY preview van een sync")
    ap.add_argument("--target", choices=["local", "prod"], default="local")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--sinds", default=None,
                    help="ondergrens registratietijdstip; default = de hele lijst "
                         "(zelfde kost, toont ook oudere achterstand)")
    ap.add_argument("--vergelijk-prod", action="store_true",
                    help="toon er ook bij wat prod mist")
    ap.add_argument("--i2a", action="store_true",
                    help="ook i2a previewen (~342 lichte RTR-calls)")
    ap.add_argument("--skip-p2p", action="store_true")
    ap.add_argument("--skip-vth", action="store_true")
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--json", action="store_true", help="machineleesbare uitvoer")
    args = ap.parse_args()
    docker_preflight()   # de DB moet er zijn voordat we iets vergelijken

    doel = kies_doelwit_db(args.target, args.dsn)
    if not args.json:
        print(f"PREVIEW (read-only) — doelwit-DB: {doel} · {datetime.date.today()}")

    uit = {}
    if not args.skip_p2p:
        uit["p2p"] = preview_p2p(args.sinds, vergelijk_prod=args.vergelijk_prod)
        if not args.json:
            print_p2p(uit["p2p"])
    if not args.skip_vth:
        uit["vth"] = preview_vth()
        if not args.json:
            print_vth(uit["vth"])
    if args.i2a:
        uit["i2a"] = preview_i2a()
        if not args.json:
            print_i2a(uit["i2a"])
    if not args.skip_embed:
        uit["embed"] = preview_embed()
        if not args.json:
            print_embed(uit["embed"])

    if args.json:
        def _plat(o):
            if isinstance(o, dict):
                return {k: _plat(v) for k, v in o.items()}
            if isinstance(o, list):
                return [_plat(v) for v in o]
            return o
        print(json.dumps(_plat(uit), ensure_ascii=False, indent=1, default=str))
    else:
        print(f"\n{'═' * 100}\nNiets geladen — dit was een preview. "
              f"Draai `full_sync.py` om het echt te doen.\n{'═' * 100}")


if __name__ == "__main__":
    main()
