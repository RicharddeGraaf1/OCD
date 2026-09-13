"""Fase 1 van G-147: meet objectversies via Presenteren v8 registratiegegevens.

Read-only. Raakt de database niet (alleen SELECT), doet per vigerende regeling
één annotatie-call met `_expand=true&_expandScope=registratiegegevens`, en
rapporteert:

  1. per objecttype de verdeling van `geregistreerdMet.versie`;
  2. hoeveel objecten al een `eindGeldigheid` dragen (= een toekomstige
     regelingversie is geregistreerd die het object raakt);
  3. de invariant "geen objectversie zonder nieuwe regelingversie": objecten
     met een `tijdstipRegistratie` NA dat van de expressie zelf. Hoort 0 te
     zijn; elke treffer is materiaal voor de revisie-vraag (vault G-147);
  4. de set-diff DB <-> API per work: objecten die lokaal aan de vigerende
     expressie hangen maar niet (meer) in de respons zitten (kandidaat
     obsoleet), en objecten in de respons die lokaal onbekend zijn.

De set-diff is alleen betrouwbaar voor artikelstructuur-regelingen (regels,
activiteiten, gebiedsaanwijzingen, normen). Voor locaties is hij indicatief:
de respons is `locatieSelectie=primair`, terwijl lokale koppelingen ook naar
sub-locaties kunnen wijzen. Vrijetekst-regelingen krijgen alleen de
versiestatistieken (1-3).

Achtergrond: vault `analysis/Objectversie-bewuste synchronisatie via
registratiegegevens.md`, `concepts/Tijdreizen.md`, gaps G-147.

Gebruik:
    python scripts/meet_objectversies.py                      # alles (~2.000 calls)
    python scripts/meet_objectversies.py --limit 5            # rooktest
    python scripts/meet_objectversies.py --bronhouder gm0344
    python scripts/meet_objectversies.py --json logs/objectversies.json
"""

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.table import Table

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _encode_regeling_uri, _get

console = Console()

EXPAND = {"locatieSelectie": "primair", "_expand": "true",
          "_expandScope": "registratiegegevens"}

# respons-collectie -> objecttype-label
REGELTEKST_COLLECTIES = {
    "regelteksten": "regeltekst",
    "regelsVoorIedereen": "juridische_regel",
    "instructieregels": "juridische_regel",
    "omgevingswaarderegels": "juridische_regel",
    "activiteiten": "activiteit",
    "gebiedsaanwijzingen": "gebiedsaanwijzing",
    "omgevingsnormen": "norm",
    "omgevingswaarden": "norm",
    "locaties": "locatie",
    "kaarten": "kaart",
}
DIVISIE_COLLECTIES = {
    "divisieteksten": "divisietekst",
    "tekstdelen": "tekstdeel",
    "hoofdlijnen": "hoofdlijn",
    "gebiedsaanwijzingen": "gebiedsaanwijzing",
    "omgevingsnormen": "norm",
    "omgevingswaarden": "norm",
    "locaties": "locatie",
    "kaarten": "kaart",
}

# lokale objecten die aan één expressie hangen, per objecttype
DB_SETS = {
    "juridische_regel": """
        SELECT identificatie FROM p2p.juridische_regel
        WHERE regeling_expression = %(expr)s""",
    "activiteit": """
        SELECT DISTINCT ala.activiteit_id FROM p2p.activiteit_locatieaanduiding ala
        JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s AND ala.activiteit_id IS NOT NULL""",
    "gebiedsaanwijzing": """
        SELECT DISTINCT g.gebiedsaanwijzing_id FROM p2p.juridische_regel_gebiedsaanwijzing g
        JOIN p2p.juridische_regel jr ON jr.identificatie = g.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s""",
    "norm": """
        SELECT DISTINCT n.norm_id FROM p2p.juridische_regel_norm n
        JOIN p2p.juridische_regel jr ON jr.identificatie = n.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s""",
    "locatie": """
        SELECT DISTINCT ala.locatie_id FROM p2p.activiteit_locatieaanduiding ala
        JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s
        UNION
        SELECT DISTINCT ga.locatie_id FROM p2p.gebiedsaanwijzing ga
        JOIN p2p.juridische_regel_gebiedsaanwijzing g ON g.gebiedsaanwijzing_id = ga.identificatie
        JOIN p2p.juridische_regel jr ON jr.identificatie = g.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s
        UNION
        SELECT DISTINCT w.locatie_id FROM p2p.normwaarde w
        JOIN p2p.juridische_regel_norm n ON n.norm_id = w.norm_id
        JOIN p2p.juridische_regel jr ON jr.identificatie = n.juridische_regel_id
        WHERE jr.regeling_expression = %(expr)s""",
}


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _expressie_registratie(cur, expr: str, work: str) -> str | None:
    """tijdstipRegistratie van de expressie: eerst lokaal (regeling_voorkomen),
    anders via GET /regelingen/{work}."""
    cur.execute("SELECT tijdstip_registratie FROM p2p.regeling_voorkomen "
                "WHERE frbr_expression = %s", (expr,))
    row = cur.fetchone()
    if row and row["tijdstip_registratie"]:
        return row["tijdstip_registratie"].isoformat()
    try:
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}")
        if data.get("expressionId") == expr:
            return (data.get("geregistreerdMet") or {}).get("tijdstipRegistratie")
    except Exception:
        pass
    return None


def meet_regeling(cur, reg: dict, vandaag: dt.date, stats: dict, details: list) -> dict:
    work, expr = reg["frbr_work"], reg["frbr_expression"]
    vrijetekst = (reg["regelingmodel"] == "RegelingVrijetekst")
    endpoint = "divisieannotaties" if vrijetekst else "regeltekstannotaties"
    collecties = DIVISIE_COLLECTIES if vrijetekst else REGELTEKST_COLLECTIES

    try:
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}/{endpoint}",
                    params=EXPAND)
    except Exception as e:
        # Projectbesluiten (regelingmodel RegelingCompact in p2p.regeling) kunnen
        # in werkelijkheid een vrijetekststructuur hebben: het regeltekst-endpoint
        # geeft dan 400 "Deze regeling ondersteunt geen Regeltekst-objecten".
        # Gezien op 2026-09-08 bij mnre1130/mnre1182. Val terug op divisie.
        if vrijetekst or "400" not in str(e):
            raise
        vrijetekst, endpoint, collecties = True, "divisieannotaties", DIVISIE_COLLECTIES
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{_encode_regeling_uri(work)}/{endpoint}",
                    params=EXPAND)
        stats["endpoint_terugval"].append(work)
    expr_ts = _parse_ts(_expressie_registratie(cur, expr, work))

    api_ids: dict[str, set] = defaultdict(set)
    uitkomst = {"work": work, "expression": expr, "documenttype": reg["documenttype"],
                "endpoint": endpoint, "expressie_registratie": expr_ts.isoformat() if expr_ts else None,
                "objecten": 0, "zonder_registratie": 0, "jonger_dan_expressie": [],
                "toekomstig_einde": Counter(), "setdiff": {}}

    for coll, otype in collecties.items():
        for obj in data.get(coll) or []:
            ident = obj.get("identificatie")
            api_ids[otype].add(ident)
            uitkomst["objecten"] += 1
            g = obj.get("geregistreerdMet")
            if not g:
                uitkomst["zonder_registratie"] += 1
                stats["zonder_registratie"][otype] += 1
                continue
            st = stats["per_type"][otype]
            st["n"] += 1
            st["versies"][str(g.get("versie"))] += 1
            eind = g.get("eindGeldigheid")
            if eind:
                st["eind_geldigheid"] += 1
                if dt.date.fromisoformat(eind) > vandaag:
                    st["eind_toekomst"] += 1
                    uitkomst["toekomstig_einde"][eind] += 1
                else:
                    st["eind_verleden"] += 1   # zou niet in een 'vandaag'-respons horen
            if g.get("eindRegistratie"):
                st["eind_registratie"] += 1
            ts = _parse_ts(g.get("tijdstipRegistratie"))
            if expr_ts and ts and ts > expr_ts:
                st["jonger_dan_expressie"] += 1
                uitkomst["jonger_dan_expressie"].append(
                    {"type": otype, "id": ident, "versie": g.get("versie"),
                     "tijdstipRegistratie": g.get("tijdstipRegistratie")})

    if not vrijetekst:
        for otype, sql in DB_SETS.items():
            cur.execute(sql, {"expr": expr})
            db_ids = {list(r.values())[0] for r in cur.fetchall()}
            api = api_ids.get(otype, set())
            alleen_db = db_ids - api
            alleen_api = api - db_ids
            uitkomst["setdiff"][otype] = {"db": len(db_ids), "api": len(api),
                                          "alleen_db": len(alleen_db), "alleen_api": len(alleen_api)}
            sd = stats["setdiff"][otype]
            sd["db"] += len(db_ids); sd["api"] += len(api)
            sd["alleen_db"] += len(alleen_db); sd["alleen_api"] += len(alleen_api)
            if alleen_db:
                sd["works_met_alleen_db"] += 1
                details.append({"work": work, "type": otype, "alleen_db": sorted(alleen_db)[:50]})

    uitkomst["toekomstig_einde"] = dict(uitkomst["toekomstig_einde"])
    return uitkomst


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="alleen de eerste N regelingen (rooktest)")
    ap.add_argument("--bronhouder", help="alleen deze bronhouder-code (bv. gm0344)")
    ap.add_argument("--documenttype", help="alleen dit documenttype")
    ap.add_argument("--json", help="schrijf het volledige rapport naar dit pad")
    args = ap.parse_args()

    vandaag = dt.date.today()
    stats = {
        "per_type": defaultdict(lambda: {"n": 0, "versies": Counter(), "eind_geldigheid": 0,
                                         "eind_toekomst": 0, "eind_verleden": 0,
                                         "eind_registratie": 0, "jonger_dan_expressie": 0}),
        "zonder_registratie": Counter(),
        "setdiff": defaultdict(lambda: {"db": 0, "api": 0, "alleen_db": 0, "alleen_api": 0,
                                        "works_met_alleen_db": 0}),
        "endpoint_terugval": [],
    }
    details: list = []
    per_regeling: list = []
    fouten: list = []

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = ("SELECT frbr_work, frbr_expression, documenttype, regelingmodel, bronhouder "
                   "FROM p2p.regeling WHERE NOT inactief")
            params: list = []
            if args.bronhouder:
                sql += " AND bronhouder = %s"; params.append(args.bronhouder)
            if args.documenttype:
                sql += " AND documenttype = %s"; params.append(args.documenttype)
            sql += " ORDER BY bronhouder, frbr_work"
            if args.limit:
                sql += " LIMIT %s"; params.append(args.limit)
            cur.execute(sql, params)
            regelingen = cur.fetchall()
        console.print(f"[bold]{len(regelingen)} vigerende regelingen[/bold] "
                      f"(peildatum {vandaag}, read-only)")

        with conn.cursor() as cur:
            for i, reg in enumerate(regelingen, 1):
                try:
                    per_regeling.append(meet_regeling(cur, reg, vandaag, stats, details))
                except Exception as e:  # één mislukte call mag de meting niet stoppen
                    fouten.append({"work": reg["frbr_work"], "fout": str(e)[:200]})
                    console.print(f"  [red]fout[/red] {reg['frbr_work']}: {str(e)[:120]}")
                if i % 50 == 0:
                    console.print(f"  {i}/{len(regelingen)}")
    finally:
        conn.close()

    # ── rapport ──
    t = Table(title="Objectversies per type (vigerende regelingen)")
    for col in ("type", "n", "versies (top 5)", "eindGeldigheid", "  toekomst", "  verleden",
                "eindRegistratie", "jonger dan expressie"):
        t.add_column(col, justify="right" if col != "type" and col != "versies (top 5)" else "left")
    for otype, st in sorted(stats["per_type"].items()):
        top = ", ".join(f"v{v}:{n}" for v, n in st["versies"].most_common(5))
        t.add_row(otype, str(st["n"]), top, str(st["eind_geldigheid"]), str(st["eind_toekomst"]),
                  str(st["eind_verleden"]), str(st["eind_registratie"]),
                  f"[red]{st['jonger_dan_expressie']}[/red]" if st["jonger_dan_expressie"] else "0")
    console.print(t)

    if stats["zonder_registratie"]:
        console.print(f"[yellow]objecten zonder geregistreerdMet:[/yellow] {dict(stats['zonder_registratie'])}")

    t2 = Table(title="Set-diff DB <-> API (artikelstructuur; locatie indicatief)")
    for col in ("type", "db", "api", "alleen in DB (kandidaat obsoleet)", "alleen in API (onbekend lokaal)",
                "works met alleen-DB"):
        t2.add_column(col, justify="left" if col == "type" else "right")
    for otype, sd in sorted(stats["setdiff"].items()):
        t2.add_row(otype, str(sd["db"]), str(sd["api"]), str(sd["alleen_db"]), str(sd["alleen_api"]),
                   str(sd["works_met_alleen_db"]))
    console.print(t2)

    jonger = [j for r in per_regeling for j in r["jonger_dan_expressie"]]
    console.print(f"\n[bold]Invariant-check[/bold] (object geregistreerd ná zijn expressie): "
                  f"{'[red]' if jonger else '[green]'}{len(jonger)}[/] treffers "
                  f"in {sum(1 for r in per_regeling if r['jonger_dan_expressie'])} regelingen")
    for r in per_regeling:
        if r["jonger_dan_expressie"]:
            console.print(f"  {r['work']}  (expressie geregistreerd {r['expressie_registratie']})")
            for j in r["jonger_dan_expressie"][:5]:
                console.print(f"     {j['type']:18s} v{j['versie']}  {j['tijdstipRegistratie']}  {j['id']}")
    zonder_ts = sum(1 for r in per_regeling if not r["expressie_registratie"])
    if zonder_ts:
        console.print(f"  [yellow]{zonder_ts} regelingen zonder bekend expressie-tijdstip "
                      f"(invariant daar niet toetsbaar; draai load-voorkomens)[/yellow]")

    toekomst = Counter()
    for r in per_regeling:
        for d, n in r["toekomstig_einde"].items():
            toekomst[d] += n
    if toekomst:
        console.print("\n[bold]Toekomstige einddatums[/bold] (objecten die een al geregistreerde "
                      "toekomstige regelingversie raakt):")
        for d, n in sorted(toekomst.items())[:15]:
            console.print(f"  {d}: {n} objecten")
    if stats["endpoint_terugval"]:
        console.print(f"\n[yellow]{len(stats['endpoint_terugval'])} regelingen met regelingmodel "
                      f"RegelingCompact bleken vrijetekst (terugval op divisieannotaties):[/yellow]")
        for w in stats["endpoint_terugval"]:
            console.print(f"  {w}")
    if fouten:
        console.print(f"\n[red]{len(fouten)} regelingen mislukt[/red]")
        for f in fouten:
            console.print(f"  {f['work']}: {f['fout'][:90]}")

    if args.json:
        out = {"peildatum": vandaag.isoformat(), "regelingen": len(per_regeling), "fouten": fouten,
               "per_type": {k: {**v, "versies": dict(v["versies"])} for k, v in stats["per_type"].items()},
               "zonder_registratie": dict(stats["zonder_registratie"]),
               "setdiff": dict(stats["setdiff"]), "alleen_db_details": details,
               "endpoint_terugval": stats["endpoint_terugval"],
               "per_regeling": per_regeling}
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        console.print(f"\nrapport: {args.json}")


if __name__ == "__main__":
    main()
