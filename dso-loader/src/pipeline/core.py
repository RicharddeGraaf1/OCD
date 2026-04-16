"""core-keten: DDL + lookup-tabellen.

Eenmalige bootstrap. De data is referentiemateriaal (waardelijsten,
bronhouder-stam) — niet bronhouder-specifiek.
"""

from rich.console import Console

from src.db import get_conn, execute_sql_file
from src.ddl import DDL, LOOKUPS

console = Console()


def bootstrap() -> None:
    """Maak schema's, tabellen en lookup-data aan. Idempotent."""
    conn = get_conn()
    try:
        console.print("[bold]core.bootstrap[/bold] — schema's + tabellen")
        execute_sql_file(conn, DDL)
        console.print("[bold]core.bootstrap[/bold] — lookup-tabellen vullen")
        execute_sql_file(conn, LOOKUPS)
        console.print("[green]core.bootstrap voltooid[/green]")
    finally:
        conn.close()
