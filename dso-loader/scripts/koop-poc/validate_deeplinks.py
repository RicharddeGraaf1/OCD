"""
Valideer alle nog-ongevalideerde deeplinks in vth.vergunning_deeplink.

Werkt op rijen met `gevalideerd_at IS NULL`. Per URL: HTTP HEAD (fallback
GET), volgt redirects, schrijft http_status / final_url / content_length /
gevalideerd_at terug.

Idempotent + restartable: bij crash kun je gewoon opnieuw starten, hij
pakt waar hij gebleven is.

Performance: globaal 0.4s tussen requests; jeleefomgeving.nl (had veel
timeouts in de eerste backfill) krijgt 1.2s om de host niet te overbelasten.

Usage:
    python validate_deeplinks.py                       # alles ongevalideerd
    python validate_deeplinks.py --limit 500           # eerste 500 (test)
    python validate_deeplinks.py --host jeleefomgeving.nl   # alleen 1 host
    python validate_deeplinks.py --retry-errors        # records met status=NULL na eerdere validate
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
DSO_LOADER_ROOT = ROOT.parent.parent
if str(DSO_LOADER_ROOT) not in sys.path:
    sys.path.insert(0, str(DSO_LOADER_ROOT))

from src.db import get_conn  # noqa: E402
from deeplinks import make_client, validate_url  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("validate_deeplinks")

DEFAULT_INTERVAL = 0.4
HOST_INTERVALS = {
    "jeleefomgeving.nl": 1.2,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Verwerk eerste N rijen (default: alles)")
    p.add_argument("--host", default=None,
                   help="Beperk tot één host")
    p.add_argument("--retry-errors", action="store_true",
                   help="Pak alleen rijen die wel gevalideerd zijn maar "
                        "http_status IS NULL hebben (timeouts/conn-errors).")
    p.add_argument("--commit-every", type=int, default=200,
                   help="Commit elke N rijen (default 200)")
    args = p.parse_args()

    conn = get_conn()
    with conn.cursor() as cur:
        if args.retry_errors:
            sql = (
                "SELECT id, koop_id, inzage_url, host "
                "FROM vth.vergunning_deeplink "
                "WHERE gevalideerd_at IS NOT NULL AND http_status IS NULL"
            )
        else:
            sql = (
                "SELECT id, koop_id, inzage_url, host "
                "FROM vth.vergunning_deeplink "
                "WHERE gevalideerd_at IS NULL"
            )
        params: list = []
        if args.host:
            sql += " AND host = %s"
            params.append(args.host)
        sql += " ORDER BY id"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql, params)
        todo = cur.fetchall()

    log.info("Te valideren: %d rijen", len(todo))
    if not todo:
        conn.close()
        return 0

    # Verdeling per host laten zien
    from collections import Counter
    host_counter = Counter(r["host"] for r in todo)
    for h, c in host_counter.most_common():
        sleep = HOST_INTERVALS.get(h, DEFAULT_INTERVAL)
        log.info("  %-35s %6d rijen  (sleep %.2fs)", h, c, sleep)

    client = make_client()
    n_ok = n_404 = n_other = n_err = 0
    t_start = time.time()

    try:
        cur = conn.cursor()
        for i, row in enumerate(todo, 1):
            res = validate_url(client, row["inzage_url"])
            s = res.get("status")
            if s and 200 <= s < 300:
                n_ok += 1
            elif s == 404:
                n_404 += 1
            elif s:
                n_other += 1
            else:
                n_err += 1

            cur.execute(
                "UPDATE vth.vergunning_deeplink SET "
                "  http_status = %s, "
                "  final_url = %s, "
                "  content_length = %s, "
                "  gevalideerd_at = now() "
                "WHERE id = %s",
                (s, res.get("final_url"), res.get("content_length"), row["id"]),
            )

            if i % args.commit_every == 0:
                conn.commit()
                rate = i / (time.time() - t_start)
                eta = (len(todo) - i) / rate if rate else 0
                log.info(
                    "  progress: %d/%d  (OK=%d 404=%d other=%d err=%d) "
                    "rate=%.1f/s  ETA=%dmin",
                    i, len(todo), n_ok, n_404, n_other, n_err,
                    rate, int(eta / 60),
                )

            time.sleep(HOST_INTERVALS.get(row["host"], DEFAULT_INTERVAL))
        conn.commit()
        cur.close()
    finally:
        client.close()
        conn.close()

    elapsed = time.time() - t_start
    log.info("Klaar in %.0f sec (%.0f min).", elapsed, elapsed / 60)
    log.info("Validatie totaal: OK=%d, 404=%d, andere=%d, err=%d",
             n_ok, n_404, n_other, n_err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
