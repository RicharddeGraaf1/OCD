"""Versie-historie per regeling via Presenteren v8 /voorkomens.

Vult p2p.regeling_voorkomen: per frbr_work alle versies door de tijd met hun
geldigheidsinterval — verleden, heden én toekomstige (al geregistreerde)
versies. Eén call per work; het endpoint blijft ook antwoorden voor regelingen
die uit de /regelingen-lijst zijn verdwenen (ingetrokken), dus de historie van
inactieve regelingen is gewoon laadbaar.

Semantiek (zie scripts/2026-08-24-add-regeling-voorkomen.sql):
- kennis-van-nu-snapshot: eind_geldigheid van het vigerende voorkomen wordt al
  gevuld zodra een opvolger geregistreerd is → upsert werkt bestaande rijen bij
  en verdwenen voorkomens (teruggetrokken toekomstige versies) worden per work
  verwijderd;
- de geldigheids- en inwerking-as zijn in het schema samengevoegd; de guard
  hieronder faalt hard zodra Ozon ooit ongelijke waarden levert;
- een 200 met 0 voorkomens voor een bekende work is verdacht (het endpoint
  geeft geen 404 voor onbekende ids) → warning, bestaande rijen blijven staan.
"""

from rich.console import Console

from src.config import cfg
from src.db import get_conn
from src.loaders.api_loader import _encode_regeling_uri, _get

console = Console()


class InwerkingGeldigheidMismatch(RuntimeError):
    """beginInwerking != beginGeldigheid: de schema-aanname van
    p2p.regeling_voorkomen breekt. Voeg een aparte inwerking-kolom toe
    voordat deze regeling geladen kan worden."""


def _fetch_voorkomens(frbr_work: str) -> list[dict]:
    """Alle voorkomens van één work, gepagineerd (size=200 volstaat ruim:
    het maximum dat we gezien hebben is 5 versies per work)."""
    enc = _encode_regeling_uri(frbr_work)
    voorkomens = []
    for page in range(1, 50):
        data = _get(f"{cfg.PRESENTEREN_BASE}/regelingen/{enc}/voorkomens",
                    params={"page": page, "size": 200})
        voorkomens.extend(data.get("_embedded", {}).get("voorkomens", []))
        if not data.get("_links", {}).get("next", {}).get("href"):
            break
    return voorkomens


def _upsert_voorkomen(cur, frbr_work: str, v: dict) -> str | None:
    """Schrijf één voorkomen; retourneert de frbr_expression (of None bij skip)."""
    expression = v.get("expressionId")
    if not expression:
        console.print(f"    [yellow]voorkomen zonder expressionId overgeslagen "
                      f"({frbr_work})[/yellow]")
        return None

    g = v.get("geregistreerdMet") or {}
    begin_geldigheid = g.get("beginGeldigheid")
    begin_inwerking = g.get("beginInwerking")
    if begin_geldigheid != begin_inwerking:
        raise InwerkingGeldigheidMismatch(
            f"{expression}: beginGeldigheid={begin_geldigheid} maar "
            f"beginInwerking={begin_inwerking}. Ozon levert nu dus wél "
            f"terugwerkende kracht; p2p.regeling_voorkomen heeft een aparte "
            f"inwerking-kolom nodig (zie 2026-08-24-add-regeling-voorkomen.sql)."
        )

    cur.execute(
        """
        INSERT INTO p2p.regeling_voorkomen
            (frbr_expression, frbr_work, versie, begin_geldigheid,
             eind_geldigheid, tijdstip_registratie, eind_registratie,
             publicatie_id, gesynct_op)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (frbr_expression) DO UPDATE SET
            frbr_work            = EXCLUDED.frbr_work,
            versie               = EXCLUDED.versie,
            begin_geldigheid     = EXCLUDED.begin_geldigheid,
            eind_geldigheid      = EXCLUDED.eind_geldigheid,
            tijdstip_registratie = EXCLUDED.tijdstip_registratie,
            eind_registratie     = EXCLUDED.eind_registratie,
            publicatie_id        = EXCLUDED.publicatie_id,
            gesynct_op           = now()
        """,
        (expression, frbr_work,
         str(g["versie"]) if g.get("versie") is not None else None,
         begin_geldigheid, g.get("eindGeldigheid"),
         g.get("tijdstipRegistratie"), g.get("eindRegistratie"),
         v.get("publicatieID")),
    )
    return expression


def load_voorkomens(works: list[str] | None = None) -> None:
    """Sync p2p.regeling_voorkomen voor de opgegeven works
    (default: alle frbr_works uit p2p.regeling, inclusief inactieve)."""
    conn = get_conn()
    try:
        if works is None:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT frbr_work FROM p2p.regeling ORDER BY 1")
                works = [r["frbr_work"] for r in cur.fetchall()]
        console.print(f"[bold]Voorkomens-sync[/bold] voor {len(works)} works")

        totaal = 0
        leeg = []
        for i, work in enumerate(works, 1):
            voorkomens = _fetch_voorkomens(work)
            if not voorkomens:
                leeg.append(work)
                continue
            with conn.cursor() as cur:
                expressions = [e for v in voorkomens
                               if (e := _upsert_voorkomen(cur, work, v))]
                # Verdwenen voorkomens (bv. teruggetrokken toekomstige versie)
                cur.execute(
                    "DELETE FROM p2p.regeling_voorkomen "
                    "WHERE frbr_work = %s AND NOT (frbr_expression = ANY(%s))",
                    (work, expressions),
                )
                if cur.rowcount:
                    console.print(f"    [yellow]{cur.rowcount} verdwenen "
                                  f"voorkomen(s) verwijderd voor {work}[/yellow]")
            conn.commit()
            totaal += len(voorkomens)
            if i % 100 == 0:
                console.print(f"  {i}/{len(works)} works, {totaal} voorkomens")

        console.print(f"[green]Klaar:[/green] {totaal} voorkomens over "
                      f"{len(works) - len(leeg)} works")
        if leeg:
            console.print(f"[yellow]{len(leeg)} works zonder voorkomens "
                          f"(bestaande rijen ongemoeid gelaten):[/yellow]")
            for w in leeg[:10]:
                console.print(f"    {w}")
    finally:
        conn.close()
