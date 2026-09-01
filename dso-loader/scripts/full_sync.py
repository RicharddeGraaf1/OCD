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
LOG_PATH = ROOT / "scripts" / f"full_sync_{VANDAAG}.log"
# Pad wordt in main() verfijnd met het label: op één dag draaien we vaak zowel
# een lokale als een prod-run, en die overschreven elkaars rapport (2026-08-01:
# het lokale rapport was weg voordat iemand het gelezen had).
REPORT_PATH = ROOT / "scripts" / f"SYNC-REPORT-{VANDAAG}.md"

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


def gemeten_subproc(args: list[str], omschrijving: str, telling_sql: str,
                    eenheid: str, timeout: int | None = None) -> tuple[bool, str]:
    """`subproc` met een deelmeting eromheen: duur, aangroei en tempo.

    Waarom dit er is. De sync van 2026-08-07 duurde 4,3 uur, waarvan
    **enrich-koop 98 minuten** en de **vth-geometrie-backfill 38 minuten** —
    samen goed voor de helft. Van geen van beide bestond een meting: het
    rapport zei alleen "ok". Zonder aantal en tempo weet je niet of 98 minuten
    veel is (5.813 kennisgevingen ≈ 1/s, terwijl de preview zelf 4/s als tempo
    noemt) of gewoon het werk.

    Dat is dezelfde blinde vlek die `refresh_drieslag` had: het runbook noemde
    5,5 min terwijl de fase 23 minuten kostte, omdat een deelmeting als
    faseduur werd gelezen.

    `telling_sql` moet één getal opleveren dat vóór en ná vergeleken kan
    worden — het aantal rijen dat deze stap zou moeten aanvullen.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        voor = q1(cur, telling_sql)
    finally:
        conn.close()

    t0 = time.monotonic()
    ok = subproc(args, omschrijving, timeout=timeout)
    duur = time.monotonic() - t0

    conn = get_conn()
    try:
        cur = conn.cursor()
        na = q1(cur, telling_sql)
    finally:
        conn.close()

    delta = (na or 0) - (voor or 0)
    tempo = f"{delta / duur:.1f} {eenheid}/s" if duur > 0 and delta else "—"
    regel = (f"- {omschrijving}: {'ok' if ok else 'FOUT'} · {duur / 60:.1f} min · "
             f"+{delta:,} {eenheid} · {tempo}")
    log(f"METING {omschrijving}: {duur / 60:.1f} min, +{delta} {eenheid}, {tempo}")
    return ok, regel


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
    # De engine moet er zijn voordat get_conn() het probeert. Twee syncs op rij
    # (21-08 en 28-08) begonnen met een dode Docker; preview_sync.py draagt de
    # helper, inclusief de WSL-aanwijzing voor het geval dat `docker ps` hangt
    # in plaats van te falen.
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from preview_sync import docker_preflight
        docker_preflight()
    except Exception as e:
        log(f"docker-preflight overgeslagen: {str(e)[:60]}")
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
    info["statistieken_hersteld"] = herstel_statistieken_na_herstart()
    log(f"Preflight ok: DB {info['db_size']}, {info['regelingen']} regelingen, "
        f"{info['disk_vrij_gb']} GB vrij")
    return info


# Tabellen die groot zijn, elke sync wisselen en daarna vooral gelézen worden.
# Precies de combinatie waarbij een lege statistiek-boekhouding het meeste kost.
HETE_TABELLEN = [
    "v2a.tekst_embedding", "v2a.chunk_categorie", "v2a.chunk_annotatie",
    "v2a.artikel_indeling", "v2a.hertaling",
    "p2p.tekst_element", "p2p.locatie", "p2p.juridische_regel",
    "p2p.locatie_basisgeo", "p2p.tekst_inline_referentie",
    "irm.screening_cel", "irm.judge_uniek",
]


def herstel_statistieken_na_herstart() -> int:
    """ANALYZE de hete tabellen als de statistiek-boekhouding leeg is.

    PostgreSQL 16 houdt de cumulatieve statistieken in gedeeld geheugen en gooit
    ze weg bij een onreine afsluiting. Docker Desktop lag er op 2026-08-21 én
    2026-08-28 uit; op die tweede datum startte de postmaster drie minuten vóór
    de sync met **159 van de 195 tabellen** zonder `last_autoanalyze`. Autovacuum
    ziet dan overal "sinds de laatste analyse niets gewijzigd" en doet niets — een
    tabel die na de herstart veel gelezen maar weinig geschreven wordt, komt zo
    nooit boven de drempel.

    Wat dat kostte, gemeten op `tier1_screen.py`: **> 30 s/regel** tegen 2,36
    s/regel na een `ANALYZE` van 81 s. Zie vault G-133.

    We analyseren alleen als het nodig lijkt (postmaster jonger dan de oudste
    `last_autoanalyze`, of helemaal geen autoanalyse bekend), zodat een gewone run
    hier geen minuut aan verliest.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        n_leeg = q1(cur, """
            SELECT count(*) n FROM pg_stat_user_tables
             WHERE last_autoanalyze IS NULL AND last_analyze IS NULL""")
        n_totaal = q1(cur, "SELECT count(*) n FROM pg_stat_user_tables")
        if n_totaal and n_leeg / n_totaal < 0.5:
            return 0
        start = q1(cur, "SELECT pg_postmaster_start_time() n")
        log(f"Statistieken grotendeels leeg ({n_leeg}/{n_totaal} tabellen, "
            f"postmaster sinds {start:%Y-%m-%d %H:%M}) — hete tabellen analyseren")
        conn.autocommit = True
        cur.execute("SET max_parallel_maintenance_workers = 0")
        gedaan = 0
        for tabel in HETE_TABELLEN:
            try:
                t0 = time.time()
                cur.execute(f"ANALYZE {tabel}")
                gedaan += 1
                log(f"  ANALYZE {tabel}: {time.time() - t0:.0f}s")
            except Exception as e:
                log(f"  ANALYZE {tabel} overgeslagen: {str(e)[:70]}")
        return gedaan
    finally:
        conn.close()


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


def leg_verwachting_vast(run_id: int, skip_p2p: bool, skip_vth: bool) -> None:
    """Zet de preview-uitkomst als verwachting in audit.sync_run.metrics.

    Zonder dit is de regressiecheck beperkt tot "groeit deze bron nog"; mét
    verwachting kan hij zeggen "de preview zag er 10 en er kwamen er 3". Dat is
    de preview-vs-uitkomst-controle die het runbook §5 als belangrijkste
    openstaande verbetering noemde — en de enige die stille onvolledigheid
    binnen één run vangt.

    Best-effort: een onbereikbare DSO mag een sync niet tegenhouden, dus bij een
    fout blijft de verwachting leeg en valt de check terug op de historie.
    """
    verwacht: dict[str, int] = {}
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        if not skip_p2p:
            from preview_sync import preview_p2p
            p = preview_p2p(sinds=None)
            verwacht["ozon-regelingen"] = len(p["nieuw"]) + len(p["nieuwe_versie"])
        if not skip_vth:
            from preview_sync import preview_vth
            v = preview_vth()
            verwacht["koop-sru-vergunningen"] = sum(
                n for _, n in v["dagen"] if isinstance(n, int))
    except Exception as e:
        log(f"verwachting vastleggen overgeslagen: {e}")
        return

    if not verwacht:
        return
    from psycopg.types.json import Json
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE audit.sync_run "
            "   SET metrics = coalesce(metrics, '{}'::jsonb) || jsonb_build_object('verwacht', %s::jsonb) "
            " WHERE run_id = %s",
            (Json(verwacht), run_id))
        conn.commit()
    finally:
        conn.close()
    log("verwachting uit preview: " + ", ".join(f"{k}={v}" for k, v in verwacht.items()))


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
    """Ondergrens voor de p2p-delta-sweep — NIET MEER DE DEFAULT.

    = start van de vorige geslaagde sync minus 2 dagen marge (overlap is
    onschadelijk dankzij de skip-guard). Valt terug op 90 dagen als er geen
    geslaagde sync bekend is. Retourneert ISO-8601 UTC ("…Z").

    Sinds 2026-08-08 draait `fase_p2p` standaard over de volledige lijst en
    roept hij deze functie niet meer aan; zie de toelichting daar. Bewaard voor
    wie bewust een venster wil zetten en voor de vth-fase, die wél een echte
    dag-watermark heeft (`vth.etl_run`).
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


def fase_p2p(bronhouders, sinds: str | None = None, full: bool = False,
             gewijzigd: set[str] | None = None) -> dict[str, str]:
    """Harvest de Ow-regelingen. Rekenwerk (subdiv) gaat naar de post-fase.

    `gewijzigd` wordt gevuld met de bronhouder-codes die daadwerkelijk iets
    geladen hebben, zodat `fase_post` de `locatie_subdiv`-herbouw gebundeld en
    alleen voor die bronhouders draait.
    """
    from src.pipeline import p2p
    from src.run_log import load_run
    if full:
        log(f"p2p VOLLEDIGE sweep over {len(bronhouders)} bronhouders (per bronhouder pollen)")
        scope = f"full-sweep:{len(bronhouders)} bronhouders"
    else:
        # Geen watermark meer als default. `find_regelingen_delta` pagineert de
        # volledige lijst hoe dan ook (~10 calls); `sinds` filtert daarná op
        # registratietijdstip en bespaart dus geen enkele API-call — het levert
        # alleen risico op. En dat risico is echt: bij de sync van 2026-08-07
        # hadden 7 van de 10 te laden regelingen een registratietijdstip van
        # 2-10 juli, ruim vóór de watermark van 29 juli. Ze waren ná de vorige
        # run in de DSO-lijst verschenen mét een oud tijdstip, en de run van
        # 1 augustus (die met --sinds 2026-06-01 draaide) had ze dus óók niet
        # gezien. Een watermark op tijdstipRegistratie veronderstelt dat een
        # item zichtbaar wordt wanneer het geregistreerd is, en dat klopt niet.
        #
        # De skip-guard doet het echte filterwerk: een al geladen expressie
        # wordt in ~1,1 ms herkend, dus ~1.960 keer niets doen kost seconden.
        # `--sinds` blijft bestaan om het venster bewust te knijpen.
        if sinds:
            log(f"p2p delta-sweep (registratietijdstip >= {sinds}) over {len(bronhouders)} bronhouders")
            scope = f"delta:sinds {sinds}"
        else:
            log(f"p2p delta-sweep (volledige lijst) over {len(bronhouders)} bronhouders")
            scope = "delta:volledige lijst"
    with load_run("ozon-regelingen", scope=scope) as run:
        if full:
            resultaten = p2p.run(bronhouders, uitstel_subdiv=True, gewijzigd=gewijzigd)
        else:
            resultaten = p2p.run_delta(bronhouders, sinds,
                                       uitstel_subdiv=True, gewijzigd=gewijzigd)
        ok = sum(1 for v in resultaten.values() if v == "ok")
        err = {k: v for k, v in resultaten.items() if v != "ok"}
        run.set(n_fout=len(err), n_verwerkt=len(resultaten))
        run.markeer_bronhouder(*[k for k, v in resultaten.items() if v == "ok"])
    for k, v in err.items():
        fouten.append(f"p2p {k}: {v}")
    if full:
        detail = f"- {ok}/{len(resultaten)} bronhouders ok"
    else:
        venster = f"sinds {sinds}" if sinds else "over de volledige lijst"
        detail = f"- {ok} bronhouders met nieuwe regelingen {venster} (rest ongewijzigd)"
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
    regels = [f"- {ok}/{len(resultaten)} ok, fouten: {len(err)}"]
    if err:
        # De codes hier laten staan is het verschil tussen "iemand ziet het" en
        # "de bronhouder draagt stil de stand van vóór de sync". core.load_run
        # sluit af op 'deels' en verder klaagt niets. Gemeten 2026-08-28: acht
        # bronhouders vielen na vijf retries alsnog om op een 503, en kwamen er
        # daarna in 45 seconden alsnog door -- transiënt dus, maar niet vanzelf.
        codes = " ".join(sorted(k for k in err if not k.startswith("__")))
        regels.append(f"- **HERSTEL NODIG** — deze bronhouders dragen nog de stand "
                      f"van vóór deze sync:")
        regels.append(f"  `python scripts/i2a_herstel_bronhouders.py {codes}`")
        fouten.append(f"i2a: {len(err)} bronhouders niet geladen — "
                      f"herstel met scripts/i2a_herstel_bronhouders.py {codes}")
    rapporteer("i2a (IMTR toepasbare regels)", regels)


# ── Fase 5: vth ──────────────────────────────────────────────────────

def fase_vth():
    """KOOP-kennisgevingen + BOPA-snapshot.

    Draait binnen een `load_run` zodat de fase in het data-actualiteit-dashboard
    verschijnt. Zonder die registratie stond `koop-sru-vergunningen` daar op
    4 juli terwijl de data t/m 31 juli geladen was (geconstateerd 2026-08-01) —
    het dashboard loog dus over juist de bron die dagelijks vers is.
    """
    from src.run_log import load_run
    with load_run("koop-sru-vergunningen", scope="sync:dagen sinds watermark") as run:
        n_fout = _fase_vth_werk()
        run.set(n_fout=n_fout)


def _fase_vth_werk() -> int:
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
    n_fout = 0
    if vanaf > vandaag:
        regels.append("- KOOP-kennisgevingen: al bij (geen nieuwe dagen)")
        ok1 = ok2 = True
    else:
        ok1 = subproc([py, "-m", "src.cli", "load-koop",
                       "--from", vanaf.isoformat(), "--to", vandaag.isoformat()],
                      f"load-koop {vanaf}..{vandaag}")
        ok2, meting_enrich = gemeten_subproc(
            [py, "-m", "src.cli", "enrich-koop", "--loop",
             "--sleep", "60", "--stop-after-empty", "2"],
            "enrich-koop",
            "SELECT count(*) n FROM vth.vergunningkennisgeving "
            "WHERE inhoud_geladen_at IS NOT NULL",
            "kennisgevingen", timeout=4 * 3600)
        # De geometrie-backfill is rekenwerk en draait in fase_post — anders
        # zit er een rekenstap tussen twee harvest-stappen (enrich → load-ovg).
        regels.append(
            f"- KOOP-kennisgevingen {vanaf}..{vandaag}: load {'ok' if ok1 else 'FOUT'} "
            f"(geometrie-backfill draait in post)")
        regels.append(meting_enrich)

    # DSO-afwijkvergunningen (BOPA): full-snapshot, idempotent — altijd verversen.
    ok_ovg = subproc([py, "-m", "src.cli", "load-ovg"], "load-ovg (afwijkvergunningen)")
    regels.append(f"- DSO-afwijkvergunningen (BOPA): {'ok' if ok_ovg else 'FOUT'}")
    rapporteer("vth (vergunningen)", regels)
    return sum(0 if ok else 1 for ok in (ok1, ok2, ok_ovg))


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


def fase_post(run_start: datetime.datetime, gewijzigd: set[str] | None = None):
    """Alle afgeleide berekeningen, ná het harvesten.

    Principe "harvest eerst, rekenen later": zolang de post-fase draait is er
    geen enkele bron meer nodig. Dat sluit het API-venster vroeg (minder kans
    op de 503's die de DSO 's nachts geeft), laat harvest-fouten meteen boven
    komen, en maakt het rekenwerk apart plan- en overslaanbaar.
    """
    from src.run_log import load_run
    py = sys.executable

    # 1. Uitgestelde subdiv-herbouw, gebundeld en alleen voor bronhouders die
    #    daadwerkelijk iets geladen hebben (zie fase_p2p / gaps G-93).
    if gewijzigd:
        with load_run("locatie-subdiv", scope=f"{len(gewijzigd)} gewijzigde bronhouders") as run:
            from src.loaders.subdiv import refresh_locatie_subdiv
            conn = get_conn()
            n_fout = 0
            totaal = 0
            for code in sorted(gewijzigd):
                try:
                    totaal += refresh_locatie_subdiv(conn, code)
                except Exception as e:
                    n_fout += 1
                    fouten.append(f"locatie_subdiv {code}: {e}")
            conn.close()
            run.set(n_verwerkt=len(gewijzigd), n_fout=n_fout)
        log(f"locatie_subdiv gebundeld ververst: {len(gewijzigd)} bronhouders, {totaal} stukjes")

        # De generalisatie hangt direct aan subdiv en hoort dus in dezelfde adem.
        # Tot 2026-09-01 draaide `vul_locatie_generalisatie.py` in geen enkele
        # sync-stap, waardoor ocd-api/tiles.py voor z0-z10 op een steeds oudere
        # laag werkte: prod liep 719.428 rijen achter zonder dat iets faalde.
        # Per bronhouder, dus geen TRUNCATE en geen lege tegellaag onderweg.
        # Gemeten 01-09 op de tien bronhouders van de sync van 28-08: 1,8 min,
        # tegen 16 min voor een volledige herbouw.
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from vul_locatie_generalisatie import BRONNEN, NIVEAUS, bouw_bronhouders
            with load_run("locatie-generalisatie",
                          scope=f"bronhouders:{len(gewijzigd)}") as run:
                bouw_bronhouders(BRONNEN["ow"], NIVEAUS, sorted(gewijzigd))
                run.set(n_verwerkt=len(gewijzigd))
        except Exception as e:
            fouten.append(f"locatie_generalisatie: {e}")
            log(f"locatie_generalisatie MISLUKT: {str(e)[:100]}")
        rapporteer("locatie_subdiv (uitgesteld naar post)", [
            f"- {len(gewijzigd)} bronhouders herbouwd, {totaal} stukjes",
        ])

    # 2. Vth-geometrie-backfill: rekenwerk, hoort dus hier en niet tussen twee
    #    harvest-stappen van fase_vth in.
    backfill = ROOT / "scripts" / "koop-poc" / "backfill_geometrie.py"
    if backfill.exists():
        ok_bf, meting_bf = gemeten_subproc(
            [py, str(backfill), "--apply"], "vth geometrie-backfill",
            "SELECT count(*) n FROM vth.vergunningkennisgeving "
            "WHERE geometrie_rd_pt IS NOT NULL",
            # Deze stap vúlt niets aan maar hérkiest: hij vervangt een verkeerd
            # gekozen gebiedsmarkering door de betrouwbaarste (G-87), en dat is
            # een UPDATE — de telling blijft dus per definitie gelijk. Met
            # "geometrieën" als eenheid las "+0" als een gat in de dekking,
            # terwijl het juist betekent dat de live loader al goed koos.
            # Gemeten 2026-08-13: +0 correcties bij 96% puntdekking op de
            # nieuwe dagen, in lijn met de dagen ervoor.
            "kennisgevingen met punt — deze stap corrigeert, +0 is goed nieuws")
        rapporteer("vth geometrie-backfill", [meting_bf])

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

    # Categorie/subcategorie/typeBepaling opnieuw bepalen. Hoort HIER en niet in
    # fase_embed: het is een opzoeking op de opschriftketen, dus geen Ollama
    # nodig — draait de embed-fase niet, dan is de indeling er tóch.
    #
    # Volledige herbouw en niet incrementeel. Twee redenen: het kost een minuut
    # over 148k artikelen, en `artikel_indeling` heeft een FK met ON DELETE
    # CASCADE op `p2p.tekst_element`. Een herladen regeling verliest daardoor
    # stil zijn indeling, en een incrementele stap die alleen naar NIEUWE
    # artikelen kijkt zou dat gat niet dichten. Precies het soort stille
    # achteruitgang waar het onderwerp-filter niets van laat zien: wat het niet
    # kent, toont het gewoon niet.
    # Dezelfde run doet ook v2a.wijziging_indeling voor de wijzigingentour —
    # één script, want liepen de regelsets uiteen dan zou hetzelfde artikel in
    # het register een ander onderwerp krijgen dan in de tour ernaast.
    with load_run("indeling", scope="categorie + typeBepaling, register + wijzigingen") as run:
        ok_ind, meting_ind = gemeten_subproc(
            [py, str(ROOT / "scripts" / "bouw_indeling.py")],
            "indeling (categorie + typeBepaling)",
            "SELECT count(*) n FROM v2a.artikel_indeling WHERE categorie IS NOT NULL",
            "ingedeelde artikelen")
        run.set(n_fout=0 if ok_ind else 1)

    conn = get_conn()
    cur = conn.cursor()
    gat = q1(cur, """
        SELECT count(*) n FROM p2p.tekst_element te
        JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
        LEFT JOIN v2a.artikel_indeling ai ON ai.tekst_element_id = te.id
        WHERE te.element_type = 'Artikel' AND NOT coalesce(r.inactief, false)
          AND ai.tekst_element_id IS NULL""")
    # Zelfde vraag voor de renvooi-kant. Hier is er géén FK die kan casseren —
    # de sleutel is (regeling_work, artikel_wid), juist om de cascade-val te
    # vermijden — maar een mislukte herbouw laat de tour wél verouderen, en die
    # klaagt daar net zo min over als het register.
    gat_wijz = q1(cur, """
        SELECT count(*) n
        FROM   p2pwijziging.tekst_element te
        JOIN   p2pwijziging.besluit b USING (ontwerpbesluit_id)
        LEFT JOIN v2a.wijziging_indeling wi
               ON wi.regeling_work = b.regeling_work AND wi.artikel_wid = te.wid
        WHERE  te.element_type = 'Artikel' AND te.wid IS NOT NULL
          AND  (te.wijzigactie IS NOT NULL OR te.vervallen OR te.bevat_renvooi)
          AND  wi.artikel_wid IS NULL""")
    conn.close()
    rapporteer("Indeling", [
        meting_ind,
        f"- artikelen in vigerende regelingen zónder indelingsrij: {gat}",
        f"- gewijzigde artikelen zónder indelingsrij: {gat_wijz}",
        "", "> Beide horen 0 te zijn. Anders is de herbouw stukgelopen en tonen",
        "> register en tour die artikelen zonder categorie, zonder dat iets klaagt.",
    ])
    if gat:
        fouten.append(f"artikel-indeling: {gat} artikelen zonder indelingsrij")
    if gat_wijz:
        fouten.append(f"wijziging-indeling: {gat_wijz} artikelen zonder indelingsrij")

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

    # De vectorlaag wisselt elke sync duizenden rijen en wordt daarna vooral
    # gelezen — precies de tabel waar verouderde statistieken het duurst zijn.
    # 81 s hier bespaarde op 2026-08-28 een factor 13 op de doorwerkingsmeting
    # (> 30 s/regel → 2,36 s/regel). Zie vault G-133.
    n_analyze = 0
    try:
        conn_a = get_conn()
        conn_a.autocommit = True
        cur_a = conn_a.cursor()
        cur_a.execute("SET max_parallel_maintenance_workers = 0")
        for tabel in ("v2a.tekst_embedding", "v2a.chunk_categorie",
                      "v2a.chunk_annotatie", "v2a.artikel_indeling"):
            t0 = time.time()
            cur_a.execute(f"ANALYZE {tabel}")
            n_analyze += 1
            log(f"ANALYZE {tabel}: {time.time() - t0:.0f}s")
        conn_a.close()
    except Exception as e:
        fouten.append(f"ANALYZE vectorlaag: {e}")

    regels = [f"- nieuw geladen regelingen deze run: {nieuw}",
              f"- DB-grootte na sync: {db_size}",
              f"- regelingen inactief/totaal: {inactief[0]}/{inactief[1]}",
              f"- ANALYZE op de vectorlaag: {n_analyze} tabellen"]
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


# ── Fase 7: embeddings + onderwerp-as ────────────────────────────────

def _vectorstand() -> dict:
    """Kerncijfers van de vectorlaag. Los van wat `run_overnight.py` zelf in
    MORNING-REPORT.md schrijft: dat bestand hoort bij een ander script en zegt
    niets meer als een fase halverwege omvalt. Deze telling komt uit de
    database en is dus waar, ook bij een gedeeltelijke run."""
    stand = {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        for sleutel, sql in [
            ("chunks", "SELECT count(*) n FROM v2a.tekst_embedding"),
            ("annotaties", "SELECT count(*) n FROM v2a.chunk_annotatie"),
            ("toewijzingen", "SELECT count(*) n FROM v2a.chunk_categorie"),
            ("categorieen", "SELECT count(*) n FROM v2a.categorie WHERE status='bevestigd'"),
        ]:
            try:
                cur.execute(sql)
                stand[sleutel] = cur.fetchone()["n"]
            except Exception:
                conn.rollback()
                stand[sleutel] = None
        conn.close()
    except Exception as e:
        log(f"vectorstand niet op te halen: {e}")
    return stand


def _verschil(voor: dict, na: dict, sleutel: str, label: str) -> str:
    a, b = voor.get(sleutel), na.get(sleutel)
    if a is None or b is None:
        return f"- {label}: niet gemeten"
    return f"- {label}: {a:,} -> {b:,} ({b - a:+,})".replace(",", ".")


def fase_embed():
    from src.run_log import load_run
    import httpx
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        httpx.get(f"{base}/api/tags", timeout=5).raise_for_status()
    except Exception:
        # Geen stille regel in het rapport: zonder deze stap is de sync
        # compleet en de vindlaag niet. Nieuwe regelingen zijn dan niet
        # semantisch vindbaar en missen hun onderwerp-categorie — en dat
        # laatste is onzichtbaar, want het onderwerp-filter in het register
        # laat wat het niet kent gewoon weg.
        rapporteer("Embeddings & onderwerp-as", [
            f"- **OVERGESLAGEN**: Ollama niet bereikbaar op {base}",
            "- Gevolg: nieuw geladen regelingen hebben geen chunks, dus geen",
            "  semantische vindbaarheid en geen onderwerp-categorie in het register.",
        ])
        fouten.append(f"embeddings overgeslagen: Ollama niet bereikbaar op {base}")
        log("Embeddings overgeslagen: Ollama niet bereikbaar")
        return

    voor = _vectorstand()
    with load_run("embeddings", scope="vectors (run_overnight)") as run:
        ok = subproc([sys.executable, str(ROOT / "scripts" / "run_overnight.py")],
                     "embeddings (run_overnight)", timeout=10 * 3600)
        run.set(n_fout=0 if ok else 1)
    na = _vectorstand()

    rapporteer("Embeddings & onderwerp-as", [
        f"- run_overnight.py: {'ok' if ok else 'FOUT (zie log)'}",
        _verschil(voor, na, "chunks", "chunks (incrementeel: alleen nieuwe tekst_elementen)"),
        _verschil(voor, na, "annotaties", "chunk_annotatie (volledige herbouw)"),
        _verschil(voor, na, "toewijzingen", "chunk_categorie (volledige herbouw)"),
        _verschil(voor, na, "categorieen", "bevestigde categorieen (hoort gelijk te blijven)"),
        "",
        "> Chunks worden alleen TOEGEVOEGD, nooit opgeruimd: chunks van een",
        "> verdrongen expressie blijven staan. Het onderwerp-endpoint zeeft ze",
        "> er bij het lezen uit op wId. Zie G-97.",
    ])
    if voor.get("categorieen") is not None and voor.get("categorieen") != na.get("categorieen"):
        fouten.append(
            f"aantal bevestigde categorieen veranderde tijdens de sync "
            f"({voor['categorieen']} -> {na['categorieen']}) — curatie gecontroleerd?"
        )


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
    ap.add_argument("--preview", action="store_true",
                    help="READ-ONLY: toon per bron wat er geladen zou worden en stop. "
                         "Raakt de database niet (geen snapshot, geen load_run, "
                         "geen rapport); alleen de logregel wordt weggeschreven.")
    args = ap.parse_args()

    # Rapportnaam uniek per run: datum + label. Zonder dit overschrijft een
    # tweede run van dezelfde dag (typisch: eerst lokaal, dan prod) het rapport
    # van de eerste.
    global REPORT_PATH
    veilig_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.label).strip("-")
    if veilig_label and veilig_label != f"full-sync-{VANDAAG}":
        REPORT_PATH = ROOT / "scripts" / f"SYNC-REPORT-{VANDAAG}-{veilig_label}.md"

    kies_doelwit_db(args)

    if args.preview:
        # Read-only pre-flight: zie scripts/preview_sync.py. Bewust vóór elke
        # schrijfactie — de sync begint anders met een snapshot + load_run-rij.
        import preview_sync
        preview_sync.main_vanuit_sync(args)
        return

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
    leg_verwachting_vast(run_id, args.skip_p2p, args.skip_vth)

    bronhouders = bouw_bronhouderlijst()
    # Harvest eerst (p2p → i2a → vth), dan pas rekenen (post → embed).
    gewijzigd: set[str] = set()
    if not args.skip_p2p:
        fase_p2p(bronhouders, sinds=args.sinds, full=args.full_p2p, gewijzigd=gewijzigd)
    if not args.skip_i2a:
        fase_i2a(bronhouders)
    if not args.skip_vth:
        fase_vth()
    if not args.skip_post:
        fase_post(run_start, gewijzigd=gewijzigd)
    elif gewijzigd:
        log(f"LET OP: --skip-post, maar {len(gewijzigd)} bronhouders wachten nog "
            f"op een locatie_subdiv-herbouw ({', '.join(sorted(gewijzigd))})")
        fouten.append(f"locatie_subdiv niet ververst voor {len(gewijzigd)} bronhouders "
                      f"(--skip-post); draai `python -m src.cli refresh-subdiv`")
    if not args.skip_embed:
        fase_embed()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE audit.sync_run SET klaar_op = now(), opmerking = %s WHERE run_id = %s",
                (f"{len(fouten)} fouten", run_id))
    conn.commit()
    conn.close()

    # Regressiecheck: niet "draaide de fase" maar "deed de fase iets". Zie
    # src/sync_regressie.py — dit vangt de nul-die-als-succes-telt.
    try:
        from src.sync_regressie import rapport_sectie
        regels = rapport_sectie(run_id)
        rapporteer("Regressiecheck (aangroei t.o.v. eerdere runs)", regels)
        for r in regels:
            if "⚠️" in r:
                fouten.append(f"regressie: {r.lstrip('- ').replace('**','')}")
    except Exception as e:
        rapporteer("Regressiecheck (aangroei t.o.v. eerdere runs)",
                   [f"- check zelf faalde: {e}"])

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
