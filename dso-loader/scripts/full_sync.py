"""Volledige nachtelijke sync in één run: p2p (incrementeel) + i2a + vth
+ post-processing + embeddings, met behoud van de actualiteit van de
vorige sync in audit.*_hist.

Draai vanuit dso-loader root:
    python scripts/full_sync.py [--skip-p2p] [--skip-i2a] [--skip-vth]
                                [--skip-post] [--skip-embed] [--label TEKST]

Doelwit-DB (nieuw):
    --target local (default) | prod   — prod leest PROD_DB_URL uit .env en
                                        draait de sync DIRECT tegen de Railway-
                                        prod-DB (via de TCP-proxy). Vraagt een
                                        typbevestiging tenzij --yes.
    --dsn <connectstring>             — expliciete DB (overschrijft --target).

  Prod-directe DELTA (aanbevolen; snel, alleen nieuwe registraties):
      python scripts/full_sync.py --target prod --skip-i2a --skip-vth
  i2a/vth hebben nog geen delta en pollen álle bronhouders → over de proxy
  traag; laat die (voorlopig) via de lokale sync + restore lopen, of draai ze
  gericht. Zie gaps G-94.

Fasen:
  0. preflight — DB / API-key / schijfruimte
  1. snapshot  — actualiteit vorige sync naar audit.regeling_load_hist,
                 audit.bronhouder_status_hist, audit.bronhouder_health_hist
  2. dedup     — 2026-07-17-sync-actualiteit-en-dedup.sql (idempotent)
  3. p2p       — alle 342 gemeenten (core.gemeentegrens) + pv/ws/mn
                 (core.bronhouder); skip-guard slaat al geladen expressies over
  4. i2a       — IMTR per gemeente + landelijke werkzaamhedencatalogus
  5. vth       — KOOP-kennisgevingen vanaf laatste ok-dag + enrich +
                 geometrie-backfill
  6. post      — regeling_load-backfill, repair-pons, ponsenkaart-stats,
                 drieslag-MV's, mv_bronhouder_health, healthrapport
  7. embed     — nieuwe chunks via run_overnight.py (resumable; skipt
                 automatisch als Ollama niet draait)

Wro zit er bewust NIET in: dat is een landelijke PDOK-herparse (16 GB
planobject met DO UPDATE = zware table-bloat) en een eigen operatie.

Rapport: scripts/SYNC-REPORT-<datum>.md
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.db import get_conn  # noqa: E402

VANDAAG = datetime.date.today().isoformat()
REPORT_PATH = ROOT / "scripts" / f"SYNC-REPORT-{VANDAAG}.md"
LOG_PATH = ROOT / "scripts" / f"full_sync_{VANDAAG}.log"

report: list[str] = [f"# Sync-rapport {VANDAAG}", ""]
fouten: list[str] = []


def log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def rapporteer(kop: str, regels: list[str]):
    report.append(f"## {kop}")
    report.extend(regels)
    report.append("")


def q1(cur, sql, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    if row is None:
        return None
    vals = list(row.values()) if hasattr(row, "values") else list(row)
    return vals[0] if len(vals) == 1 else vals


def subproc(args: list[str], omschrijving: str, timeout: int | None = None) -> bool:
    """Draai een subproces, stream output naar het logbestand."""
    log(f"START {omschrijving}: {' '.join(args)}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        try:
            res = subprocess.run(args, cwd=ROOT, env=env, stdout=f,
                                 stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            fouten.append(f"{omschrijving}: timeout na {timeout}s")
            log(f"TIMEOUT {omschrijving}")
            return False
    if res.returncode != 0:
        fouten.append(f"{omschrijving}: exit {res.returncode} (zie log)")
        log(f"FOUT {omschrijving}: exit {res.returncode}")
        return False
    log(f"KLAAR {omschrijving}")
    return True


# ── Fase 0: preflight ────────────────────────────────────────────────

def preflight() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    info = {
        "db_size": q1(cur, "SELECT pg_size_pretty(pg_database_size(current_database())) s"),
        "regelingen": q1(cur, "SELECT count(*) n FROM p2p.regeling"),
        "ala": q1(cur, "SELECT count(*) n FROM p2p.activiteit_locatieaanduiding"),
    }
    conn.close()
    vrij = shutil.disk_usage("c:/").free / 1e9
    info["disk_vrij_gb"] = round(vrij)
    if vrij < 20:
        raise SystemExit(f"Te weinig schijfruimte: {vrij:.0f} GB vrij")
    if not os.environ.get("DSO_API_KEY"):
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        if not os.environ.get("DSO_API_KEY"):
            raise SystemExit("DSO_API_KEY ontbreekt in omgeving/.env")
    log(f"Preflight ok: DB {info['db_size']}, {info['regelingen']} regelingen, "
        f"{info['disk_vrij_gb']} GB vrij")
    return info


# ── Fase 1+2: snapshot + dedup ───────────────────────────────────────

def snapshot_en_dedup(label: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    # DDL + dedup + unieke indexen (idempotent)
    sql = (ROOT / "scripts" / "2026-07-17-sync-actualiteit-en-dedup.sql").read_text(encoding="utf-8")
    cur.execute(sql)
    conn.commit()

    cur.execute("INSERT INTO audit.sync_run (label) VALUES (%s) RETURNING run_id", (label,))
    run_id = list(cur.fetchone().values())[0]

    cur.execute("INSERT INTO audit.regeling_load_hist SELECT rl.*, %s, now() FROM p2p.regeling_load rl", (run_id,))
    n_rl = cur.rowcount
    cur.execute("INSERT INTO audit.bronhouder_status_hist SELECT b.*, %s, now() FROM core.bronhouder b", (run_id,))
    n_bh = cur.rowcount
    try:
        cur.execute("INSERT INTO audit.bronhouder_health_hist SELECT h.*, %s, now() FROM core.mv_bronhouder_health h", (run_id,))
        n_health = cur.rowcount
    except Exception as e:
        conn.rollback()
        n_health = 0
        fouten.append(f"health-snapshot mislukt: {e}")
    conn.commit()

    dup_check = q1(cur, """SELECT count(*) n FROM (
        SELECT 1 FROM p2p.activiteit_locatieaanduiding
        GROUP BY juridische_regel_id, activiteit_id, locatie_id, kwalificatie
        HAVING count(*) > 1) d""")
    conn.close()
    rapporteer("Snapshot & dedup", [
        f"- run_id: {run_id} (label `{label}`)",
        f"- regeling_load-snapshot: {n_rl} rijen → audit.regeling_load_hist",
        f"- bronhouder-snapshot: {n_bh} rijen → audit.bronhouder_status_hist",
        f"- health-snapshot: {n_health} rijen → audit.bronhouder_health_hist",
        f"- ALA-dubbelgroepen na dedup: {dup_check} (hoort 0 te zijn)",
    ])
    log(f"Snapshot klaar (run_id {run_id}), dedup-restgroepen: {dup_check}")
    return run_id


# ── Fase 3: p2p ──────────────────────────────────────────────────────

def bouw_bronhouderlijst():
    from src.pipeline.bronhouders import Bronhouder, _infer_type
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT overheidscode, naam FROM core.gemeentegrens ORDER BY overheidscode")
    gemeenten = [(r["overheidscode"], r["naam"]) for r in cur.fetchall()]
    cur.execute("""SELECT overheidscode, naam FROM core.bronhouder
                   WHERE overheidscode NOT LIKE 'gm%' ORDER BY overheidscode""")
    overig = [(r["overheidscode"], r["naam"]) for r in cur.fetchall()]
    conn.close()

    lijst = []
    for code, naam in gemeenten:
        kale = code[2:] if code.startswith("gm") else code
        lijst.append(Bronhouder(code=kale, naam=naam, type="gemeente"))
    for code, naam in overig:
        lijst.append(Bronhouder(code=code, naam=naam, type=_infer_type(code)))
    return lijst


def bepaal_sinds() -> str:
    """Ondergrens voor de p2p-delta-sweep.

    = start van de vorige geslaagde sync minus 2 dagen marge (overlap is
    onschadelijk dankzij de skip-guard). Valt terug op 90 dagen als er geen
    geslaagde sync bekend is. Retourneert ISO-8601 UTC ("…Z").
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        sinds = q1(cur, """
            SELECT to_char((max(gestart_op) AT TIME ZONE 'UTC') - interval '2 days',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            FROM audit.sync_run
            WHERE klaar_op IS NOT NULL
              AND coalesce(opmerking,'') NOT ILIKE '%%afgebroken%%'
        """)
    finally:
        conn.close()
    if not sinds:
        val = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)
        sinds = val.strftime("%Y-%m-%dT%H:%M:%SZ")
    return sinds


def fase_p2p(bronhouders, sinds: str | None = None, full: bool = False) -> dict[str, str]:
    from src.pipeline import p2p
    from src.run_log import load_run
    if full:
        log(f"p2p VOLLEDIGE sweep over {len(bronhouders)} bronhouders (per bronhouder pollen)")
        scope = f"full-sweep:{len(bronhouders)} bronhouders"
    else:
        sinds = sinds or bepaal_sinds()
        log(f"p2p delta-sweep (registratietijdstip >= {sinds}) over {len(bronhouders)} bronhouders")
        scope = f"delta:sinds {sinds}"
    with load_run("ozon-regelingen", scope=scope) as run:
        if full:
            resultaten = p2p.run(bronhouders)
        else:
            resultaten = p2p.run_delta(bronhouders, sinds)
        ok = sum(1 for v in resultaten.values() if v == "ok")
        err = {k: v for k, v in resultaten.items() if v != "ok"}
        run.set(n_fout=len(err), n_verwerkt=len(resultaten))
        run.markeer_bronhouder(*[k for k, v in resultaten.items() if v == "ok"])
    for k, v in err.items():
        fouten.append(f"p2p {k}: {v}")
    if full:
        detail = f"- {ok}/{len(resultaten)} bronhouders ok"
    else:
        detail = f"- {ok} bronhouders met nieuwe regelingen sinds {sinds} (rest ongewijzigd)"
    rapporteer("p2p (Ow-regelingen)", [
        detail,
        f"- fouten: {len(err)}" + (f" — {', '.join(list(err)[:15])}" if err else ""),
    ])
    return resultaten


# ── Fase 4: i2a ──────────────────────────────────────────────────────

def fase_i2a(bronhouders):
    from src.pipeline import i2a
    from src.run_log import load_run
    with load_run("rtr-toepasbare-regels", scope="sync:gemeenten") as run:
        resultaten = i2a.run(bronhouders)
        ok = sum(1 for v in resultaten.values() if v == "ok")
        err = {k: v for k, v in resultaten.items() if v != "ok"}
        run.set(n_fout=len(err))
    for k, v in err.items():
        fouten.append(f"i2a {k}: {v}")
    rapporteer("i2a (IMTR toepasbare regels)", [
        f"- {ok}/{len(resultaten)} ok, fouten: {len(err)}",
    ])


# ── Fase 5: vth ──────────────────────────────────────────────────────

def fase_vth():
    py = sys.executable
    conn = get_conn()
    cur = conn.cursor()
    laatste = q1(cur, "SELECT max(processed_date)::text m FROM vth.etl_run WHERE status='ok'")
    conn.close()
    vandaag = datetime.date.today()
    if laatste:
        vanaf = datetime.date.fromisoformat(laatste) + datetime.timedelta(days=1)
    else:
        vanaf = vandaag - datetime.timedelta(days=7)

    regels = []
    if vanaf > vandaag:
        regels.append("- KOOP-kennisgevingen: al bij (geen nieuwe dagen)")
    else:
        ok1 = subproc([py, "-m", "src.cli", "load-koop",
                       "--from", vanaf.isoformat(), "--to", vandaag.isoformat()],
                      f"load-koop {vanaf}..{vandaag}")
        ok2 = subproc([py, "-m", "src.cli", "enrich-koop", "--loop",
                       "--sleep", "60", "--stop-after-empty", "2"],
                      "enrich-koop", timeout=4 * 3600)
        backfill = ROOT / "scripts" / "koop-poc" / "backfill_geometrie.py"
        ok3 = True
        if backfill.exists():
            ok3 = subproc([py, str(backfill), "--apply"], "vth geometrie-backfill")
        regels.append(
            f"- KOOP-kennisgevingen {vanaf}..{vandaag}: load {'ok' if ok1 else 'FOUT'}, "
            f"enrich {'ok' if ok2 else 'FOUT'}, geometrie-backfill {'ok' if ok3 else 'FOUT'}")

    # DSO-afwijkvergunningen (BOPA): full-snapshot, idempotent — altijd verversen.
    ok_ovg = subproc([py, "-m", "src.cli", "load-ovg"], "load-ovg (afwijkvergunningen)")
    regels.append(f"- DSO-afwijkvergunningen (BOPA): {'ok' if ok_ovg else 'FOUT'}")
    rapporteer("vth (vergunningen)", regels)


# ── Fase 6: post-processing ──────────────────────────────────────────

REGELING_LOAD_BACKFILL = """
INSERT INTO p2p.regeling_load (frbr_expression, status, n_tekst, geladen_op)
SELECT r.frbr_expression,
       CASE WHEN coalesce(t.n, 0) = 0 THEN 'partieel' ELSE 'ok' END,
       coalesce(t.n, 0),
       now()
FROM p2p.regeling r
LEFT JOIN (SELECT regeling_expression, count(*) n
           FROM p2p.tekst_element GROUP BY 1) t
       ON t.regeling_expression = r.frbr_expression
ON CONFLICT (frbr_expression) DO UPDATE
    SET status  = EXCLUDED.status,
        n_tekst = EXCLUDED.n_tekst
"""


def fase_post(run_start: datetime.datetime):
    from src.run_log import load_run
    py = sys.executable
    conn = get_conn()
    cur = conn.cursor()
    # geladen_op van bestaande rijen blijft staan (DO UPDATE raakt alleen
    # status/n_tekst); nieuwe regelingen krijgen now().
    cur.execute(REGELING_LOAD_BACKFILL)
    conn.commit()
    nieuw = q1(cur, "SELECT count(*) n FROM p2p.regeling_load WHERE geladen_op >= %s",
               (run_start,))
    conn.close()

    # Post-stappen elk in een load_run zodat ze mét duur in het
    # data-actualiteit-dashboard verschijnen (voorheen onzichtbaar, terwijl de
    # drieslag-MV-refresh de langste fase van de hele sync is).
    with load_run("post-processing", scope="repair-pons + ponsenkaart-stats") as run:
        ok = subproc([py, "-m", "src.cli", "repair-pons-placeholders"], "repair-pons-placeholders")
        ok = subproc([py, "-m", "src.cli", "refresh-ponsenkaart-stats"], "refresh-ponsenkaart-stats") and ok
        run.set(n_fout=0 if ok else 1)

    with load_run("drieslag-mv", scope="naammatch/tekst-object/gio-consistentie MV's") as run:
        ok = subproc([py, str(ROOT / "scripts" / "refresh_drieslag.py")], "drieslag-MV-refresh",
                     timeout=3 * 3600)
        run.set(n_fout=0 if ok else 1)

    with load_run("health-mv", scope="mv_bronhouder_health + mv_geo_health") as run:
        nfout = 0
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("REFRESH MATERIALIZED VIEW core.mv_bronhouder_health")
            conn.commit()
        except Exception as e:
            conn.rollback()
            fouten.append(f"mv_bronhouder_health refresh: {e}")
            nfout += 1
        try:
            cur.execute("REFRESH MATERIALIZED VIEW core.mv_geo_health")
            conn.commit()
        except Exception as e:
            conn.rollback()
            fouten.append(f"mv_geo_health refresh: {e}")
            nfout += 1
        conn.close()
        run.set(n_fout=nfout)

    # Report-metingen (buiten de load_runs; snel).
    conn = get_conn()
    cur = conn.cursor()
    health = None
    try:
        cur.execute("SELECT * FROM core.v_data_health")
        health = cur.fetchall()
    except Exception:
        conn.rollback()
    db_size = q1(cur, "SELECT pg_size_pretty(pg_database_size(current_database())) s")
    inactief = q1(cur, """SELECT count(*) FILTER (WHERE inactief) i, count(*) t
                          FROM p2p.regeling""")
    conn.close()

    regels = [f"- nieuw geladen regelingen deze run: {nieuw}",
              f"- DB-grootte na sync: {db_size}",
              f"- regelingen inactief/totaal: {inactief[0]}/{inactief[1]}"]
    if health:
        regels.append(f"- v_data_health: {health}")
    rapporteer("Post-processing", regels)


def snapshot_totalen(run_id: int):
    """Leg de per-bron totaalstanden + kern-metrics van deze run vast op
    audit.sync_run, zodat het dashboard per run het verschil met de vorige
    run kan tonen."""
    from psycopg.types.json import Json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bron, totaal FROM core.v_bron_totalen")
    totalen = {r["bron"]: r["totaal"] for r in cur.fetchall()}
    cur.execute("SELECT count(*) c, count(*) FILTER (WHERE inactief) i FROM p2p.regeling")
    reg = cur.fetchone()
    db = q1(cur, "SELECT pg_size_pretty(pg_database_size(current_database())) s")
    metrics = {"regelingen": reg["c"], "regelingen_inactief": reg["i"], "db_grootte": db}
    cur.execute("UPDATE audit.sync_run SET totalen=%s, metrics=%s WHERE run_id=%s",
                (Json(totalen), Json(metrics), run_id))
    conn.commit()
    conn.close()
    log(f"Totaal-snapshot vastgelegd voor run {run_id} ({metrics['regelingen']} regelingen, {db})")


# ── Fase 7: embeddings ───────────────────────────────────────────────

def fase_embed():
    from src.run_log import load_run
    import httpx
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        httpx.get(f"{base}/api/tags", timeout=5).raise_for_status()
    except Exception:
        rapporteer("Embeddings", [f"- OVERGESLAGEN: Ollama niet bereikbaar op {base}"])
        log("Embeddings overgeslagen: Ollama niet bereikbaar")
        return
    with load_run("embeddings", scope="vectors (run_overnight)") as run:
        ok = subproc([sys.executable, str(ROOT / "scripts" / "run_overnight.py")],
                     "embeddings (run_overnight)", timeout=10 * 3600)
        run.set(n_fout=0 if ok else 1)
    rapporteer("Embeddings", [f"- run_overnight.py: {'ok' if ok else 'FOUT (zie log)'}"])


# ── Doelwit-DB (lokaal vs prod-direct) ───────────────────────────────

def _masker_dsn(dsn: str) -> str:
    """Verberg wachtwoord/gebruiker in een connectstring voor logging."""
    return re.sub(r"://[^:/@]+(:[^@]+)?@", "://***@", dsn)


def kies_doelwit_db(args):
    """Zet OCD_DB_URL op basis van --dsn/--target vóór enige DB-connectie.

    Local (default) = ongewijzigd (cfg bouwt de DSN uit DB_HOST-delen). Prod
    leest PROD_DB_URL uit .env en draait de sync DIRECT tegen de Railway-prod-DB.
    Een prod-doelwit vereist een typbevestiging tenzij --yes, want we schrijven
    dan rechtstreeks in productie. get_conn() zet bij een prod-DSN automatisch
    parallelisme uit (Railway /dev/shm).
    """
    dsn = args.dsn
    if not dsn and args.target == "prod":
        from dotenv import dotenv_values
        dsn = (dotenv_values(ROOT / ".env") or {}).get("PROD_DB_URL")
        if not dsn:
            raise SystemExit("PROD_DB_URL ontbreekt in .env — kan niet tegen prod draaien.")
    if not dsn:
        log("Doelwit-DB: LOKAAL (default)")
        return
    dsn = dsn.strip().strip('"').strip("'")
    os.environ["OCD_DB_URL"] = dsn
    prod = ("rlwy.net" in dsn) or ("railway" in dsn) or (args.target == "prod")
    log(f"Doelwit-DB: {'PROD (direct!)' if prod else 'EXPLICIET'} → {_masker_dsn(dsn)}")
    report.insert(1, f"> **Doelwit-DB:** {'PRODUCTIE (direct)' if prod else _masker_dsn(dsn)}")
    if prod and not args.yes:
        try:
            antwoord = input(
                "\n⚠  Je gaat DIRECT tegen PRODUCTIE schrijven. "
                "Typ exact 'PROD' om door te gaan: ")
        except EOFError:
            raise SystemExit("Non-interactief zonder --yes; afgebroken.")
        if antwoord.strip() != "PROD":
            raise SystemExit("Afgebroken door gebruiker.")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-p2p", action="store_true")
    ap.add_argument("--skip-i2a", action="store_true")
    ap.add_argument("--skip-vth", action="store_true")
    ap.add_argument("--skip-post", action="store_true")
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--full-p2p", action="store_true",
                    help="volledige per-bronhouder-sweep i.p.v. de snelle "
                         "registratietijdstip-delta (voor verse restore / integriteitscheck)")
    ap.add_argument("--sinds", default=None,
                    help="ISO-8601 UTC ondergrens voor de p2p-delta-sweep; "
                         "default = start vorige geslaagde sync − 2 dagen")
    ap.add_argument("--target", choices=["local", "prod"], default="local",
                    help="DB-doelwit; 'prod' draait direct tegen de Railway-prod-DB "
                         "(PROD_DB_URL uit .env, via TCP-proxy)")
    ap.add_argument("--dsn", default=None,
                    help="expliciete DB-connectstring; overschrijft --target")
    ap.add_argument("--yes", action="store_true",
                    help="sla de prod-bevestiging over (voor cron/non-interactief)")
    ap.add_argument("--label", default=f"full-sync-{VANDAAG}")
    args = ap.parse_args()

    kies_doelwit_db(args)

    run_start = datetime.datetime.now(datetime.timezone.utc)
    t0 = time.monotonic()
    info = preflight()
    rapporteer("Uitgangssituatie", [
        f"- DB-grootte vooraf: {info['db_size']}",
        f"- regelingen vooraf: {info['regelingen']}",
        f"- ALA-rijen vooraf: {info['ala']}",
        f"- schijf vrij: {info['disk_vrij_gb']} GB",
    ])

    run_id = snapshot_en_dedup(args.label)

    bronhouders = bouw_bronhouderlijst()
    if not args.skip_p2p:
        fase_p2p(bronhouders, sinds=args.sinds, full=args.full_p2p)
    if not args.skip_i2a:
        fase_i2a(bronhouders)
    if not args.skip_vth:
        fase_vth()
    if not args.skip_post:
        fase_post(run_start)
    if not args.skip_embed:
        fase_embed()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE audit.sync_run SET klaar_op = now(), opmerking = %s WHERE run_id = %s",
                (f"{len(fouten)} fouten", run_id))
    conn.commit()
    conn.close()

    try:
        snapshot_totalen(run_id)
    except Exception as e:
        fouten.append(f"totaal-snapshot: {e}")

    duur = (time.monotonic() - t0) / 3600
    rapporteer("Fouten", [f"- {f}" for f in fouten] if fouten else ["- geen"])
    report.insert(2, f"**Duur:** {duur:.1f} uur · **fouten:** {len(fouten)} · run_id {run_id}")
    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")
    log(f"Sync klaar in {duur:.1f} uur, {len(fouten)} fouten. Rapport: {REPORT_PATH.name}")


if __name__ == "__main__":
    main()
