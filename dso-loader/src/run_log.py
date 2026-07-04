"""Load-run-registratie: schrijft 1 rij per laad-stap naar core.load_run.

Gebruik als context manager rond een laad-commando (fase 2 van het
data-actualiteit-dashboard, zie OCDviewer/docs/plans/data-health-dashboard.md):

    with load_run("koop-sru-vergunningen", scope="2026-05-21..2026-07-04") as run:
        total = load_koop_range(...)
        run.set(n_verwerkt=total)

Semantiek:
- Bij binnenkomst wordt een 'running'-rij geschreven (meteen zichtbaar).
- Bij normale afloop -> status 'ok' (of 'deels' als n_fout > 0), finished_at=now().
- Bij een exception -> status 'gefaald', error=traceback, en de fout wordt
  opnieuw geworpen (de loader-flow verandert niet).
- run.markeer_bronhouder(code, ...) bumpt bij succes core.bronhouder.laatst_geladen.

De registratie draait op een EIGEN autocommit-connectie, los van de
loader-transactie. Zo overleeft een 'gefaald'-rij ook als de loader zijn eigen
transactie terugrolt, en is de 'running'-rij tijdens een lange load al zichtbaar.

Kanttekening: een hard afgebroken proces (kill, stroomuitval) laat de rij op
'running' staan — het dashboard kan een 'running' ouder dan X als "afgebroken?"
tonen. `n_verwerkt` wordt alleen gezet waar de loader een telling teruggeeft;
voor de overige bronnen is het (voorlopig) NULL.
"""

import traceback
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Json

from src.config import cfg
from src.db import normalize_bronhouder_code


class RunHandle:
    """Doorgegeven aan de with-body om de run te verrijken."""

    def __init__(self) -> None:
        self.n_verwerkt: int | None = None
        self.n_fout: int = 0
        self.details: dict | None = None
        self._bronhouder_codes: list[str] = []

    def set(self, *, n_verwerkt: int | None = None,
            n_fout: int | None = None, details: dict | None = None) -> None:
        if n_verwerkt is not None:
            self.n_verwerkt = n_verwerkt
        if n_fout is not None:
            self.n_fout = n_fout
        if details is not None:
            self.details = details

    def markeer_bronhouder(self, *codes: str) -> None:
        """Registreer overheidscodes waarvan core.bronhouder.laatst_geladen bij
        succes op now() moet."""
        for code in codes:
            if code:
                self._bronhouder_codes.append(normalize_bronhouder_code(code))


def _bron_totaal(conn, bron: str) -> int | None:
    """Live totaal-telling van de doel-tabel via core.bron_totaal(). Best-effort:
    NULL/None als de functie of tabel (nog) niet bestaat."""
    try:
        row = conn.execute("SELECT core.bron_totaal(%s) AS n", (bron,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


@contextmanager
def load_run(bron: str, scope: str = "alle"):
    conn = psycopg.connect(cfg.db_url, autocommit=True)
    try:
        run_id = conn.execute(
            "INSERT INTO core.load_run (bron, scope, status) "
            "VALUES (%s, %s, 'running') RETURNING run_id",
            (bron, scope),
        ).fetchone()[0]
        totaal_voor = _bron_totaal(conn, bron)

        handle = RunHandle()
        try:
            yield handle
        except BaseException:
            conn.execute(
                "UPDATE core.load_run SET status='gefaald', finished_at=now(), "
                "error=%s, n_verwerkt=%s, n_fout=%s, details=%s WHERE run_id=%s",
                (traceback.format_exc()[:8000], handle.n_verwerkt, handle.n_fout,
                 Json(handle.details) if handle.details is not None else None, run_id),
            )
            raise
        else:
            status = "deels" if (handle.n_fout or 0) > 0 else "ok"
            totaal_na = _bron_totaal(conn, bron)

            # Details verrijken met de totaal-telling voor/na, altijd zichtbaar.
            details = dict(handle.details) if handle.details else {}
            if totaal_voor is not None:
                details["totaal_voor"] = totaal_voor
            if totaal_na is not None:
                details["totaal_na"] = totaal_na

            # n_verwerkt = 'bijgewerkt in deze run'. Loader-telling (bv. KOOP)
            # heeft voorrang; anders best-effort de netto-aangroei (totaal_na -
            # totaal_voor). Voor full-reload-bronnen (gemeentegrenzen, snapshot)
            # is die delta ~0 — dan zegt totaal_na wat er staat.
            n_verwerkt = handle.n_verwerkt
            if n_verwerkt is None and totaal_voor is not None and totaal_na is not None:
                n_verwerkt = totaal_na - totaal_voor

            conn.execute(
                "UPDATE core.load_run SET status=%s, finished_at=now(), "
                "n_verwerkt=%s, n_fout=%s, details=%s WHERE run_id=%s",
                (status, n_verwerkt, handle.n_fout,
                 Json(details) if details else None, run_id),
            )
            if handle._bronhouder_codes:
                conn.execute(
                    "UPDATE core.bronhouder SET laatst_geladen=now() "
                    "WHERE overheidscode = ANY(%s)",
                    (handle._bronhouder_codes,),
                )
    finally:
        conn.close()
