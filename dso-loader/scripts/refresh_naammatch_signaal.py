"""Refresh p2p.naammatch_signaal materialized view.

Draaien na elke loader-run waarin tekst_element.inhoud_plain of
de naam van een Activiteit/Gebiedsaanwijzing/Norm/Omgevingswaarde
is gewijzigd of toegevoegd.

CONCURRENTLY zodat readers niet geblokkeerd worden — vereist de
UNIQUE index die in 2026-05-add-naammatch-signaal.sql is aangemaakt.

Run:
  python scripts/refresh_naammatch_signaal.py
"""
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from time import time

from rich.console import Console

from src.db import get_conn

console = Console()


def refresh():
    conn = get_conn()
    try:
        t0 = time()
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY p2p.naammatch_signaal")
            cur.execute("SELECT COUNT(*) AS n FROM p2p.naammatch_signaal")
            row = cur.fetchone()
        conn.commit()
        elapsed = time() - t0
        console.print(
            f"[green]Refreshed[/green] p2p.naammatch_signaal — "
            f"{row['n']:,} rijen in {elapsed:.1f}s"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    refresh()
