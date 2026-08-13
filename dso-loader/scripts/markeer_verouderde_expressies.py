"""Eenmalige opschoning: markeer verouderde regeling-expressies als inactief.

Per frbr_work met meerdere expressies halen we bij DSO Presenteren de vigerende
expressionId op en zetten alle ANDERE expressies van dat work op inactief
(reden 'verouderde-versie'). Autoritatief — immuun voor de lexicale
sorteerfout bij versies als @9-0 vs @10-0.

Veiligheidsklep: als de door DSO gemelde vigerende expressie niet in onze DB
staat (onze data loopt achter), slaan we het work over — anders zouden we ALLE
expressies inactief zetten en het work volledig laten verdwijnen. Zo'n work is
kandidaat voor een echte (her)load, niet voor markering.

Read-mostly: enige mutatie is de inactief-markering via markeer_siblings_inactief.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# `python scripts/markeer_verouderde_expressies.py` — het commando zoals het in
# docs/synchronisatie-runbook.md §Stap 2 staat — zet alleen scripts/ op
# sys.path, niet de repo-root, dus `src` was daar onvindbaar. full_sync.py doet
# dit al (regel 54); hier ontbrak het, waardoor de gedocumenteerde aanroep
# faalde op ModuleNotFoundError en je hem alleen met PYTHONPATH=. aan de praat
# kreeg. Gevonden tijdens de sync van 2026-08-12.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.progress import track

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _get
from src.versie_status import markeer_siblings_inactief, nieuwste_expression

console = Console()


def _dso_current(work: str):
    """Vraag DSO Presenteren om de vigerende expressionId van een work.

    Retourneert None als het work niet los opvraagbaar is (404) of geen
    expressionId heeft (bv. ingetrokken)."""
    try:
        encoded = work.replace("/", "_")
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{encoded}")
        return data.get("expressionId")
    except Exception:
        return None


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT frbr_work, count(*) AS n
                FROM p2p.regeling
                GROUP BY frbr_work
                HAVING count(*) > 1
                ORDER BY frbr_work
            """)
            works = cur.fetchall()

        console.print(f"[bold]{len(works)} works met meerdere expressies[/bold]")

        gemarkeerd = 0
        via_dso = 0        # vigerende bepaald via DSO expressionId (autoritatief)
        via_parse = 0      # vigerende bepaald via numerieke versie-parse (fallback)
        fouten = 0

        for row in track(works, description="Markeren"):
            work = row["frbr_work"]
            try:
                # Onze expressies voor dit work.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT frbr_expression FROM p2p.regeling WHERE frbr_work = %s",
                        (work,),
                    )
                    lokaal = [r["frbr_expression"] for r in cur.fetchall()]

                # 1) Autoritatief: DSO-expressionId, mits die lokaal bestaat.
                current = _dso_current(work)
                bron = "dso"
                if not current or current not in lokaal:
                    # 2) Fallback: numerieke versie-parse over lokale expressies.
                    current = nieuwste_expression(lokaal)
                    bron = "parse"

                with conn.cursor() as cur:
                    n = markeer_siblings_inactief(cur, work, current)
                conn.commit()
                gemarkeerd += n
                if bron == "dso":
                    via_dso += 1
                else:
                    via_parse += 1
            except Exception as e:
                fouten += 1
                conn.rollback()
                console.print(f"  [red]fout[/red] {work[:60]}: {e}")

        console.print("\n[bold green]Klaar[/bold green]")
        console.print(f"  expressies gemarkeerd inactief : {gemarkeerd}")
        console.print(f"  vigerende via DSO (autoritatief): {via_dso}")
        console.print(f"  vigerende via versie-parse      : {via_parse}")
        console.print(f"  fouten                          : {fouten}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
