"""Monitor-data voor instructieregels.nl.

De wekelijkse build van instructieregels.nl (GitHub Actions) haalde zijn data tot
2026-10-28 rechtstreeks uit de productiedatabase. Sinds de verhuizing naar
Hetzner heeft die database geen publieke poort meer (docs/railway-naar-hetzner-plan.md),
en de build vraagt de data nu hier op, met de private key.

De query is een kopie van build/query.sql uit de repo instructieregels.nl. Dit
endpoint stuurt de SHA-1 van zijn kopie mee (X-Query-Sha1); build.sh vergelijkt
die met zijn eigen query.sql en stopt bij een verschil, zodat de twee niet
ongemerkt uit elkaar lopen. Bijwerken: kopieer build/query.sql naar
queries/instructieregels_monitor.sql en push.
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response

from db import get_conn

router = APIRouter(prefix="/v1/instructieregels", tags=["instructieregels"])

_RUW = (Path(__file__).parent / "queries" / "instructieregels_monitor.sql").read_bytes()
_RUW = _RUW.replace(b"\r\n", b"\n")  # zelfde hash op Windows en Linux
QUERY_SHA1 = hashlib.sha1(_RUW).hexdigest()

# psql-opdrachten (\set ...) zijn geen SQL. :'gendate' is een psql-variabele; hier
# een parameter. De ene % in de query (ILIKE) moet daarvoor verdubbeld worden.
_SQL = "\n".join(r for r in _RUW.decode("utf-8").splitlines() if not r.startswith("\\"))
_SQL = _SQL.strip().rstrip(";").replace("%", "%%").replace(":'gendate'", "%(gendate)s::text")
# Als tekst teruggeven, net als `psql -t -A` in build.sh: dan is de uitvoer gelijk.
_SQL = f"SELECT (q.j)::text AS monitor FROM (\n{_SQL}\n) AS q(j)"

# De query loopt ~40 s op prod; de gewone statement_timeout (20 s) is te krap.
TIMEOUT_MS = 180_000


@router.get("/monitor")
def monitor(
    request: Request,
    gendate: date = Query(..., description="Datum die als 'gegenereerd' in de uitvoer komt"),
) -> Response:
    if getattr(request.state, "tier", None) not in ("private", "anonymous"):
        raise HTTPException(status_code=403, detail="Alleen met de private key")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
        cur.execute(_SQL, {"gendate": gendate.isoformat()})
        rij = cur.fetchone()
    return Response(
        content=rij["monitor"],
        media_type="application/json",
        headers={"X-Query-Sha1": QUERY_SHA1, "Cache-Control": "no-store"},
    )
