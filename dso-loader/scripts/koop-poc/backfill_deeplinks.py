"""
Eenmalige backfill: voor alle records met `inhoud_xml IS NOT NULL`,
extract deeplink-URLs (volgens whitelist in deeplinks.py), valideer ze
met een HTTP-call, en schrijf naar vth.vergunning_deeplink.

Voor latere records: ingest.py (enrich-pass) doet de extract+insert
zonder validatie. Een aparte `validate-deeplinks` pass kan periodiek
de unvalidated rows oppikken.

Performance: ~1 URL/sec (validatie domineert). Voor ~1.300 URLs op de
huidige 69k verrijkte records: ~22 minuten.

Usage:
    cd scripts/koop-poc
    python backfill_deeplinks.py            # alles, met validatie
    python backfill_deeplinks.py --limit 50 # eerste 50 (kleine test)
    python backfill_deeplinks.py --no-validate  # snel; alleen extractie
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
DSO_LOADER_ROOT = ROOT.parent.parent  # c:/GIT/OCD/dso-loader/
if str(DSO_LOADER_ROOT) not in sys.path:
    sys.path.insert(0, str(DSO_LOADER_ROOT))

from src.db import get_conn  # noqa: E402
from deeplinks import (  # noqa: E402
    DEEPLINK_HOSTS,
    extract_deeplinks,
    make_client,
    upsert_deeplink,
    validate_url,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_deeplinks")

VALIDATION_INTERVAL = 0.4  # sec tussen HTTP-calls (sequential)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Verwerk eerste N records (default: alles)")
    p.add_argument("--no-validate", action="store_true",
                   help="Alleen extractie; sla HTTP-validatie over")
    p.add_argument("--commit-every", type=int, default=200,
                   help="Commit elke N records (default 200)")
    args = p.parse_args()

    log.info("Whitelist: %d hosts", len(DEEPLINK_HOSTS))
    for h in DEEPLINK_HOSTS:
        log.info("  %s", h)

    conn = get_conn()
    with conn.cursor() as cur:
        # Tabel-existentie verifiëren (anders krijg je een verwarrende fout)
        cur.execute(
            "SELECT to_regclass('vth.vergunning_deeplink') AS t"
        )
        if cur.fetchone()["t"] is None:
            log.error("Tabel vth.vergunning_deeplink bestaat niet — "
                      "run `python ingest.py setup` eerst.")
            return 1

        # Records ophalen
        sql = (
            "SELECT koop_id, inhoud_xml "
            "FROM vth.vergunningkennisgeving "
            "WHERE inhoud_xml IS NOT NULL "
            "ORDER BY datum_publicatie DESC"
        )
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql)
        records = cur.fetchall()

    log.info("%d records met inhoud_xml — extractie...", len(records))

    # Stap 1: extract alle (koop_id, url, bron) uit XML
    all_links: list[tuple[str, str, str]] = []
    n_with_link = 0
    for row in records:
        links = extract_deeplinks(row["inhoud_xml"])
        if links:
            n_with_link += 1
        for url, bron in links:
            all_links.append((row["koop_id"], url, bron))
    log.info(
        "Extractie: %d unieke (record,url)-paren in %d records",
        len(all_links), n_with_link,
    )

    if not all_links:
        log.info("Niets te doen.")
        conn.close()
        return 0

    # Stap 2: per URL valideren + inserten
    n_ok = n_404 = n_other = n_err = 0
    client = None if args.no_validate else make_client()
    inserted = 0
    t_start = time.time()

    try:
        cur = conn.cursor()
        for i, (koop_id, url, bron) in enumerate(all_links, 1):
            validation = None
            if client is not None:
                validation = validate_url(client, url)
                s = validation.get("status")
                if s and 200 <= s < 300:
                    n_ok += 1
                elif s == 404:
                    n_404 += 1
                elif s:
                    n_other += 1
                else:
                    n_err += 1
            upsert_deeplink(cur, koop_id, url, bron, validation)
            inserted += 1

            if i % args.commit_every == 0:
                conn.commit()
                rate = i / (time.time() - t_start) if t_start else 0
                eta = (len(all_links) - i) / rate if rate else 0
                log.info(
                    "  progress: %d/%d  (OK=%d 404=%d other=%d err=%d) "
                    "rate=%.1f/s  ETA=%dmin",
                    i, len(all_links), n_ok, n_404, n_other, n_err,
                    rate, int(eta / 60),
                )

            if client is not None:
                time.sleep(VALIDATION_INTERVAL)
        conn.commit()
        cur.close()
    finally:
        if client is not None:
            client.close()
        conn.close()

    elapsed = time.time() - t_start
    log.info("Klaar. %d rijen weggeschreven in %.1f sec.", inserted, elapsed)
    if client is not None:
        log.info("Validatie: OK=%d, 404=%d, andere status=%d, err=%d",
                 n_ok, n_404, n_other, n_err)
    log.info("Tip: 'python ingest.py status-deeplinks' voor samenvatting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
