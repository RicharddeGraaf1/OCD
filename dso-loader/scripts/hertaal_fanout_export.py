"""Stap 6bis, deel 1 — zet de te hertalen teksten klaar in batches voor subagents.

Waarom deze route naast `begrijpelijk-hertaling.py`: die roept de Anthropic-API
aan en heeft dus API-tegoed nodig. De hertalingen die er al liggen zijn zo
NIET gemaakt — de 15.455 Sonnet-rijen van 21/22 juli 2026 zijn geschreven door
subagents in Claude Code, dus op het abonnement. Dit script (en zijn tegenhanger
`hertaal_fanout_laad.py`) maakt die route herhaalbaar.

De prompt in OPDRACHT.md is woordelijk die van `begrijpelijk-hertaling.py` en is
**context-vrij**: geen regelingnaam. Dat is geen slordigheid maar de voorwaarde
voor de content-dedup — met een regelingnaam erin zou dezelfde bruidsschat-tekst
per gemeente een andere prompt en dus een andere uitkomst geven, en dan valt de
hele cache uit elkaar (3,87x dedup landelijk).

Run:  python scripts/hertaal_fanout_export.py                  # scope = laatste sync
      python scripts/hertaal_fanout_export.py --batches 6
      python scripts/hertaal_fanout_export.py --sinds 2026-08-15T19:27:31Z

Achterstand (docs/hertaal-achterstand-plan.md): de hele DB in porties, in plaats
van alleen wat de laatste sync laadde.

      python scripts/hertaal_fanout_export.py --alles --max 6000 --per-batch 120 \
          --max-lengte 3000 --dir data/hertaal-achterstand/portie-01
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import textwrap

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

# Voortgangsregels bevatten een pijl (U+2192). Op een cp1252-console gooit dat een
# UnicodeEncodeError - en als dat na de commit gebeurt, lijkt het laden mislukt
# terwijl de data er gewoon staat. Gebeurd 2026-08-22 in dit script en in
# repliceer_p2p_naar_prod.py, allebei op dezelfde dag.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HIER = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(HIER / ".env")

PROMPT_VERSIE = "v1"
ELEMENT_TYPES = ("Lid", "Divisietekst")
MIN_LEN = 30
TRUNC = 3000

SQL = """
WITH scope AS (
    SELECT frbr_expression AS expr FROM p2p.regeling_load WHERE geladen_op >= %(sinds)s
), elems AS (
    SELECT te.inhoud_plain, v2a.norm_hash(te.inhoud_plain) AS bh
    FROM   scope s JOIN p2p.tekst_element te ON te.regeling_expression = s.expr
    WHERE  te.element_type = ANY(%(types)s)
      AND  te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > %(minlen)s
)
SELECT DISTINCT ON (e.bh) e.bh, e.inhoud_plain
FROM   elems e
WHERE  NOT EXISTS (SELECT 1 FROM v2a.hertaling h
                   WHERE h.bron_hash = e.bh AND h.model = %(model)s
                     AND h.prompt_versie = %(pv)s)
ORDER  BY e.bh, length(e.inhoud_plain)
"""

# --alles: alle actieve regelingen in plaats van de laatste sync. `prio` zet de
# omgevingsplannen (gm) voorop, dan provincies, waterschappen en de rest: de
# viewer en de bot lezen vooral gemeentelijke regels, dus die komen eerst aan de beurt.
SQL_ALLES = """
WITH elems AS (
    SELECT te.inhoud_plain, v2a.norm_hash(te.inhoud_plain) AS bh,
           CASE left(split_part(te.regeling_expression, '/', 5), 2)
                WHEN 'gm' THEN 1 WHEN 'pv' THEN 2 WHEN 'ws' THEN 3 ELSE 4 END AS prio
    FROM   p2p.tekst_element te
    JOIN   p2p.regeling r ON r.frbr_expression = te.regeling_expression
    WHERE  NOT coalesce(r.inactief, false)
      AND  te.element_type = ANY(%(types)s)
      AND  te.inhoud_plain IS NOT NULL AND length(te.inhoud_plain) > %(minlen)s
      AND  length(te.inhoud_plain) <= %(maxlengte)s
)
SELECT DISTINCT ON (e.bh) e.bh, e.inhoud_plain, e.prio
FROM   elems e
WHERE  NOT EXISTS (SELECT 1 FROM v2a.hertaling h
                   WHERE h.bron_hash = e.bh AND h.model = %(model)s
                     AND h.prompt_versie = %(pv)s)
ORDER  BY e.bh, e.prio
"""

OPDRACHT = """\
Je maakt begrijpelijke varianten van juridische teksten uit de Nederlandse
omgevingsregelgeving.

Lees dit bestand: {batch}

Het is een JSON-array van objecten met `bron_hash` en `tekst` (de brontekst).

Voor ELK object schrijf je een hertaling, alsof je deze opdracht beantwoordt:

  Systeemrol: "Je bent een helper die juridische teksten uitlegt in eenvoudig
  Nederlands."
  Opdracht: "Leg de volgende tekst uit de Nederlandse omgevingsregelgeving uit
  in begrijpelijke taal voor een gewone burger. Schrijf maximaal 3-4 korte
  zinnen. Gebruik geen juridisch jargon. Geef alleen de uitleg, geen inleiding.

  Tekst:
  <de tekst>"

Regels voor de hertaling:
- Maximaal 3-4 korte zinnen. Geen inleiding zoals "Deze tekst betekent..." —
  begin direct met de uitleg.
- Geen juridisch jargon. Schrijf zoals je het aan een buurman zou uitleggen.
- Blijf inhoudelijk trouw: verzin geen regels, versoepel of verscherp niets,
  laat geen voorwaarde weg die de burger raakt.
- Nederlands. Geen markdown, geen opsommingstekens, geen aanhalingstekens rond
  de uitleg.
- Is een tekst fragmentarisch (alleen een verwijzing of een los kopje), schrijf
  dan in een zin wat er staat. Sla niets over.

Schrijf het resultaat met de Write-tool naar: {out}

Formaat: een JSON-object per regel, exact deze twee velden, niets anders:
{{"bron_hash": "<ongewijzigd overgenomen>", "tekst": "<jouw hertaling>"}}

Kritisch: kopieer `bron_hash` teken voor teken uit de invoer. Dat is de sleutel
waarop de hertaling aan de brontekst hangt; een afwijkend teken maakt de regel
onbruikbaar. Het aantal regels moet gelijk zijn aan het aantal objecten in de
invoer.

Doe dit werk ZELF. Start geen subagents en besteed niets uit: dat loopt vast
op de concurrency-limiet en levert een lege uitvoer op (gemeten 2026-09-04:
3 van de 10 agents deden dit en schreven niets weg).

Werk in porties van ongeveer 40 teksten en schrijf na elke portie weg, zodat
een onderbreking nooit het hele batchresultaat kost.

Rapporteer als eindantwoord alleen het aantal verwerkte teksten en het pad —
niet de hertalingen zelf.
"""


def db_url() -> str:
    return (f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}")



def archiveer_vorige(werk):
    """Zet een map met afgeronde uitvoer opzij in plaats van te weigeren.

    De klep hieronder is terecht -- de batches zijn óók de referentie waartegen
    het laadscript natelt, dus een tweede export erover schrijft de controle
    weg. Maar de oplossing is mechanisch: hernoem de map naar
    `<naam>-<datum-van-de-inhoud>` en begin schoon. Tot 2026-08-29 moest dat met
    de hand, en dat is precies het soort stap dat je één keer vergeet.

    De datum komt van de nieuwste `out-*.jsonl` in de map, niet van vandaag: zo
    heet het archief naar de run waar het bij hoort.
    """
    import datetime as _dt
    uitvoer = sorted(werk.glob("out-*.jsonl"))
    if not uitvoer:
        return None
    datum = _dt.date.fromtimestamp(max(f.stat().st_mtime for f in uitvoer))
    doel = werk.with_name(f"{werk.name}-{datum:%Y-%m-%d}")
    n = 2
    while doel.exists():
        doel = werk.with_name(f"{werk.name}-{datum:%Y-%m-%d}-{n}")
        n += 1
    werk.rename(doel)
    werk.mkdir(parents=True, exist_ok=True)
    return doel

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sinds", help="ISO-tijdstip; default = start laatste geslaagde sync")
    ap.add_argument("--alles", action="store_true",
                    help="alle actieve regelingen, niet alleen de laatste sync (achterstand)")
    ap.add_argument("--max", type=int, help="hooguit zoveel teksten (een portie)")
    ap.add_argument("--max-lengte", type=int,
                    help="langere teksten overslaan in plaats van afkappen (G-179); alleen met --alles")
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--per-batch", type=int,
                    help="aantal teksten per batch; bepaalt dan het aantal batches")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--dir", default=str(HIER / "data" / "hertaal"),
                    help="werkmap (default data/hertaal, staat in .gitignore)")
    ap.add_argument("--opnieuw", action="store_true",
                    help="werkmap met bestaande uitvoer toch overschrijven")
    args = ap.parse_args()

    werk = pathlib.Path(args.dir)
    werk.mkdir(parents=True, exist_ok=True)

    # De batches zijn niet alleen invoer voor de subagents maar ook de
    # referentie waartegen hertaal_fanout_laad.py natelt. Een tweede export in
    # dezelfde map (bijvoorbeeld om te controleren dat er niets meer openstaat)
    # schrijft ze leeg, en dan is er niets meer om de uitvoer tegen te houden.
    # Gebeurd op 2026-08-16; vandaar deze klep.
    if not args.opnieuw and any(werk.glob("out-*.jsonl")):
        oud = archiveer_vorige(werk)
        print(f"vorige uitvoer gearchiveerd naar {oud.name}")

    with psycopg.connect(db_url(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        if args.alles:
            print("scope: alle actieve regelingen")
            cur.execute(SQL_ALLES, {"types": list(ELEMENT_TYPES), "minlen": MIN_LEN,
                                    "maxlengte": args.max_lengte or 10**9,
                                    "model": args.model, "pv": PROMPT_VERSIE})
            rijen = sorted(cur.fetchall(), key=lambda r: (r["prio"], r["bh"]))
        else:
            sinds = args.sinds
            if not sinds:
                cur.execute("SELECT max(gestart_op) AS t FROM audit.sync_run WHERE klaar_op IS NOT NULL")
                sinds = cur.fetchone()["t"]
                print(f"scope: sinds de laatste sync-run ({sinds})")
            cur.execute(SQL, {"sinds": sinds, "types": list(ELEMENT_TYPES), "minlen": MIN_LEN,
                              "model": args.model, "pv": PROMPT_VERSIE})
            rijen = cur.fetchall()

    print(f"te hertalen: {len(rijen)} unieke teksten")
    if args.max and len(rijen) > args.max:
        rijen = rijen[:args.max]
        print(f"  waarvan deze portie: {len(rijen)}")
    if not rijen:
        return

    # Verdelen met een stap in plaats van aaneengesloten blokken: de query is op
    # lengte gesorteerd, dus blokken zouden een agent alle korte en een ander
    # alle lange teksten geven.
    n = min(args.batches, len(rijen))
    if args.per_batch:
        n = -(-len(rijen) // args.per_batch)
    for i in range(n):
        deel = rijen[i::n]
        pad = werk / f"batch-{i + 1:02d}.json"
        pad.write_text(json.dumps(
            [{"bron_hash": r["bh"], "tekst": r["inhoud_plain"][:TRUNC]} for r in deel],
            ensure_ascii=False, indent=1), encoding="utf-8")
        # De agents lezen de omgeslagen .txt: in de .json staan regels van
        # duizenden tekens die afgekapt gelezen worden (runbook stap 6bis, 17-10).
        # Tot 2026-11-06 werd die .txt elke sync met de hand gemaakt.
        delen = []
        for j, r in enumerate(deel, 1):
            regels = []
            for regel in r["inhoud_plain"][:TRUNC].splitlines() or [""]:
                regels += textwrap.wrap(regel, 110, break_long_words=True,
                                        break_on_hyphens=False) or [""]
            delen.append(f"##### ITEM {j}/{len(deel)}\nSLEUTEL: {r['bh']}\nTEKST:\n"
                         + "\n".join(regels))
        pad.with_suffix(".txt").write_text("\n\n".join(delen) + "\n", encoding="utf-8")
        (werk / f"tmp-{i + 1:02d}").mkdir(exist_ok=True)
        print(f"  {pad.name}: {len(deel)} teksten")

    opdracht = werk / "OPDRACHT.md"
    opdracht.write_text(OPDRACHT.format(batch=f"{werk}/batch-NN.json",
                                        out=f"{werk}/out-NN.jsonl"), encoding="utf-8")
    print(f"\nOpdracht voor de subagents staat in {opdracht}")
    print(f"Start {n} subagents op Sonnet, een per batch (NN = 01..{n:02d}).")
    print("Daarna: python scripts/hertaal_fanout_laad.py --ja")


if __name__ == "__main__":
    main()
