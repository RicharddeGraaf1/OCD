"""Database connection and helpers."""

import psycopg
from psycopg.rows import dict_row
from src.config import cfg


# Keten-schema map: bepaalt waar een tabel leeft na de p2p/wro/i2a/core split.
TABLE_SCHEMA: dict[str, str] = {
    # core
    "bronhouder": "core", "waardelijst": "core",
    "bestemmingshoofdgroep": "core", "dubbelbestemmingshoofdgroep": "core",
    "bouwaanduidingtype": "core", "maatvoeringsaanduiding": "core",
    "figuurtype": "core", "gebiedsaanduidinghoofdgroep": "core",
    "dossierstatus": "core", "planstatus": "core",
    "regelingmodel": "core", "besluitmodel": "core",
    "publicatiebladtype": "core", "idealisatie": "core",
    "toestemmingstype": "core", "documenttype": "core",
    # p2p
    "regeling": "p2p", "besluit": "p2p", "besluit_regeling": "p2p",
    "procedurestap": "p2p", "tekst_element": "p2p",
    "geo_informatieobject": "p2p", "juridische_borging": "p2p",
    "locatie": "p2p", "locatiegroep_lid": "p2p",
    "juridische_regel": "p2p", "activiteit": "p2p",
    "activiteit_locatieaanduiding": "p2p",
    "gebiedsaanwijzing": "p2p", "juridische_regel_gebiedsaanwijzing": "p2p",
    "norm": "p2p", "normwaarde": "p2p", "juridische_regel_norm": "p2p",
    "tekstdeel": "p2p", "hoofdlijn": "p2p", "tekstdeel_hoofdlijn": "p2p",
    "pons": "p2p", "kaart": "p2p", "kaartlaag": "p2p",
    # wro
    "wro_manifest": "wro", "wro_dossier": "wro",
    "ruimtelijk_instrument": "wro", "planobject": "wro",
    "wro_tekst_object": "wro", "wro_geleideformulier": "wro",
    "wro_bronbestand": "wro",
    "wro_snapshot": "wro", "wro_plan_observatie": "wro",
    # i2a
    "regelbeheerobject": "i2a", "toepasbaar_regelbestand": "i2a",
    "dmn_element": "i2a", "uitvoeringsregel": "i2a",
    "werkzaamheid": "i2a", "aansluitpunt": "i2a", "aansluiting": "i2a",
}


def qualify(table: str) -> str:
    """Return schema-qualified table name, e.g. 'p2p.regeling'."""
    schema = TABLE_SCHEMA.get(table)
    if schema is None:
        raise KeyError(f"Onbekende tabel voor schema-mapping: {table}")
    return f"{schema}.{table}"


def get_conn() -> psycopg.Connection:
    """Get a new database connection."""
    return psycopg.connect(cfg.db_url, row_factory=dict_row, autocommit=False)


def execute_sql_file(conn: psycopg.Connection, sql: str) -> None:
    """Execute a multi-statement SQL string."""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def normalize_bronhouder_code(code: str) -> str:
    """Normaliseer een bronhouder-code naar standaard formaat.

    Gemeenten: '0344' → 'gm0344'
    Provincies, waterschappen, rijk: 'pv26', 'ws0147', 'mnre1034' → ongewijzigd
    """
    if code and len(code) == 4 and code.isdigit():
        return f"gm{code}"
    return code


def table_count(conn: psycopg.Connection, table: str) -> int:
    """Quick row count for a table."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {qualify(table)}")  # noqa: S608
        row = cur.fetchone()
        return row["n"] if row else 0
