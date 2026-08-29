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
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

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
    ap.add_argument("--batches", type=int, default=10)
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
        sinds = args.sinds
        if not sinds:
            cur.execute("SELECT max(gestart_op) AS t FROM audit.sync_run WHERE klaar_op IS NOT NULL")
            sinds = cur.fetchone()["t"]
            print(f"scope: sinds de laatste sync-run ({sinds})")
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(SQL, {"sinds": sinds, "types": list(ELEMENT_TYPES), "minlen": MIN_LEN,
                          "model": args.model, "pv": PROMPT_VERSIE})
        rijen = cur.fetchall()

    print(f"te hertalen: {len(rijen)} unieke teksten")
    if not rijen:
        return

    # Verdelen met een stap in plaats van aaneengesloten blokken: de query is op
    # lengte gesorteerd, dus blokken zouden een agent alle korte en een ander
    # alle lange teksten geven.
    n = min(args.batches, len(rijen))
    for i in range(n):
        deel = rijen[i::n]
        pad = werk / f"batch-{i + 1:02d}.json"
        pad.write_text(json.dumps(
            [{"bron_hash": r["bh"], "tekst": r["inhoud_plain"][:TRUNC]} for r in deel],
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {pad.name}: {len(deel)} teksten")

    opdracht = werk / "OPDRACHT.md"
    opdracht.write_text(OPDRACHT.format(batch=f"{werk}/batch-NN.json",
                                        out=f"{werk}/out-NN.jsonl"), encoding="utf-8")
    print(f"\nOpdracht voor de subagents staat in {opdracht}")
    print(f"Start {n} subagents op Sonnet, een per batch (NN = 01..{n:02d}).")
    print("Daarna: python scripts/hertaal_fanout_laad.py --ja")


if __name__ == "__main__":
    main()
