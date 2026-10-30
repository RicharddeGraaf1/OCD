"""Draai de GIO-stap van api_loader opnieuw, met de ZIP van de juiste versie.

Tot 2026-10-29 cachete `_download_regeling` de ZIP per work, en een eenmaal
gedownloade ZIP werd nooit ververst. Elke nieuwe versie van een bestaand plan
kreeg daardoor de GIO's en de locatie-basisgeo van de eerste download: een scan
van de 1.967 gecachte ZIP's vond 151 vigerende regelingen met een ZIP van een
andere versie. Zie docs/sync-2026-10-29.md, bevinding 3.

Dit script haalt per expressie de ZIP opnieuw (cache per expressie, met de
controle dat de ZIP die expressie bevat) en draait `process_zip` plus
`resolve_target_soort`, net als api_loader na het laden van de tekst. Alles wat
die stap schrijft is aanvullend (ON CONFLICT DO NOTHING / COALESCE), dus een
herhaalde run is onschadelijk.

Gebruik:
    python scripts/herlaad_gio_zip.py --expressie /akn/nl/act/gm0203/2020/omgevingsplan/nld@1073
    python scripts/herlaad_gio_zip.py --expressie <a> --expressie <b> --ja

Zonder --ja alleen de lijst met wat er zou gebeuren. Schrijft naar de lokale DB;
de weg naar prod is repliceer_p2p_naar_prod.py (voor expressies van deze run).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import get_conn  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expressie", action="append", required=True)
    ap.add_argument("--ja", action="store_true")
    args = ap.parse_args()

    from src.loaders.gio_zip import process_zip
    from src.loaders.inline_referentie import resolve_target_soort
    from src.loaders.ow_loader import _download_regeling

    conn = get_conn()
    cur = conn.cursor()
    for expr in args.expressie:
        cur.execute("SELECT frbr_work, inactief FROM p2p.regeling WHERE frbr_expression = %s", (expr,))
        r = cur.fetchone()
        if not r:
            print(f"ONBEKEND   {expr}")
            continue
        if r["inactief"]:
            print(f"INACTIEF   {expr} (overgeslagen)")
            continue
        if not args.ja:
            print(f"ZOU DOEN   {expr}")
            continue
        zip_path = _download_regeling(r["frbr_work"], expr)
        if not zip_path:
            print(f"GEEN ZIP   {expr}")
            continue
        stats = process_zip(zip_path, conn, regeling_expression=expr)
        res = resolve_target_soort(conn, regeling_expression=expr)
        conn.commit()
        print(f"GEDAAN     {expr}: {stats} · resolve {res}")
    conn.close()


if __name__ == "__main__":
    main()
