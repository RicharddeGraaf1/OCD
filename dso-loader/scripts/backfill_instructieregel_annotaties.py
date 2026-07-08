"""Backfill instructieregel_instrument / _taakuitoefening als text[] uit de DSO API.

Nodig omdat api_loader.py de meervoudige DSO-JSON-keys (instructieregelInstrumenten /
instructieregelTaakuitoefeningen) eerder als enkelvoud las → NULL voor alle via de API
geladen instructieregels (Bkl/AMvB/MR). Dit script her-haalt de annotaties per regeling
en zet de arrays. Draai NA de text[]-migratie (scripts/2026-07-instructieregel-arrays.sql).

Gebruik:  python scripts/backfill_instructieregel_annotaties.py
"""
import sys, os, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, psycopg
from src.config import Config

cfg = Config()
H = {"X-Api-Key": cfg.DSO_API_KEY}


def concept_tails(items):
    out = []
    for it in items or []:
        uri = (it.get("code") or it.get("waarde")) if isinstance(it, dict) else it
        if uri:
            tail = uri.rstrip("/").split("/")[-1]
            if tail:
                out.append(tail)
    return out or None


def fetch_instructieregels(work):
    enc = work.replace("/", "_").replace("-", "_")
    url = f"{cfg.PRESENTEREN_BASE}/regelingen/{enc}/regeltekstannotaties"
    with httpx.Client(timeout=180) as c:
        r = c.get(url, headers=H, params={"locatieSelectie": "primair"})
        r.raise_for_status()
        return r.json().get("instructieregels", [])


def main():
    conn = psycopg.connect(os.environ.get("OCD_DB_URL") or cfg.db_url)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT r.frbr_work
            FROM p2p.regeling r
            JOIN p2p.juridische_regel jr ON jr.regeling_expression = r.frbr_expression
            WHERE jr.regel_type = 'Instructieregel'
              AND r.documenttype IN ('Omgevingsverordening','AMvB','Ministeriële Regeling')
        """)
        works = [row[0] for row in cur.fetchall()]
    print(f"{len(works)} regelingen met instructieregels")

    tot_upd = 0
    for i, work in enumerate(works, 1):
        try:
            irs = fetch_instructieregels(work)
        except Exception as e:
            print(f"  [{i}/{len(works)}] {work}: FOUT {e}")
            continue
        upd = 0
        with conn.cursor() as cur:
            for ir in irs:
                instr = concept_tails(ir.get("instructieregelInstrumenten"))
                taak = concept_tails(ir.get("instructieregelTaakuitoefeningen"))
                if instr is None and taak is None:
                    continue
                cur.execute("""
                    UPDATE p2p.juridische_regel
                       SET instructieregel_instrument = COALESCE(%s::text[], instructieregel_instrument),
                           instructieregel_taakuitoefening = COALESCE(%s::text[], instructieregel_taakuitoefening)
                     WHERE identificatie = %s AND regel_type = 'Instructieregel'
                """, (instr, taak, ir["identificatie"]))
                upd += cur.rowcount
        conn.commit()
        tot_upd += upd
        print(f"  [{i}/{len(works)}] {work[:55]}: {len(irs)} irs, {upd} bijgewerkt")
    print(f"\nKlaar. Totaal bijgewerkt: {tot_upd}")
    conn.close()


if __name__ == "__main__":
    main()
