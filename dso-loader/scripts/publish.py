#!/usr/bin/env python
"""publish.py — publiceer verse OCD-data naar de downstream-websites.

BEWUST GESCHEIDEN van full_sync.py. `full_sync.py` werkt de LOKALE database bij;
`publish.py` brengt die verse data naar de sites die eindgebruikers zien. Reden
voor de scheiding:
  - een deploy-fout mag de data-pijplijn nooit raken;
  - deploy-credentials (Cloudflare/Railway) horen niet in de loader;
  - de prod-restore is zwaar/destructief en hoort een bewust go-moment.

Zie: docs/synchronisatieproces_beschrijving.md en de vault-analyse
"Publicatie-pijplijn - van sync naar downstream-sites" (gaps G-94).

TWEE SPOREN
-----------
  live   : sites die RUNTIME de prod-API lezen → alleen prod verversen.
           * ponsenkaart.nl  (/v1/ponsenkaart/*, /v1/planvoorraad/*)
           * prod-OCDviewer, omgevingsbot  (volgen prod automatisch)
  baked  : sites die BUILD-TIME data bakken → herbouwen + deployen.
           * instructieregels.nl    (build/build.sh → web/data.js → Cloudflare)
           * annotatieconformiteit  (C:/GIT/annotatieconformiteit.nl, ex-odkwaliteit:
                                     collect → score → export → npm run deploy)
           * mer-register.nl        (C:/GIT/MER-register.nl: export uit de harvest-
                                     store + twee prod-verrijkers → ./deploy.sh)

  RoM-prototype staat hier NIET bij: buiten scope sinds 2026-07-24.
  De registry in sites() is de waarheid — niet deze docstring.

VEILIGHEID
----------
  * DRY-RUN is de default: toont wat het zou doen. Gebruik --execute om te draaien.
  * Per-site isolatie: een mislukte deploy stopt de andere sites niet.
  * Poort: publiceert alleen als de laatste sync-run status 'ok' had
    (overrulebaar met --force).
  * Per-site PRE-FLIGHT: een site kan een read-only check meebrengen die moet
    slagen vóór build/deploy. Instructieregels gebruikt dat om te weigeren als
    de doorwerkingsmeting achterloopt op de data — die oordelen komen uit een
    aparte lokale pijplijn, dus "de tabellen zijn gevuld" zegt niets over
    actualiteit. Zie runbook stap 6b. Draait ook in dry-run (read-only).

STATUS: alle drie de sites in sites() zijn concreet ingevuld. Wat nog scaffold is:
refresh_prod() — --prod-mode staat default op 'none', 'delta' is een TODO-stub die
alleen print, en 'restore' roept de zware destructieve restore aan. Prod hoort al
vers te zijn uit runbook stap 3/4 vóór je hier komt.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Windows-console (cp1252) struikelt over → / é; forceer UTF-8 op de output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

# publish.py las tot 2026-08-29 alleen os.environ en niet de .env die er vlak
# naast staat. Gevolg: OCD_PROD_URL was leeg terwijl dso-loader/.env dezelfde
# waarde als PROD_DB_URL bevat, en de mer-verrijkers vielen om zonder dat iemand
# begreep waarom. `override=False`, dus een expliciet gezette omgevingsvariabele
# wint nog steeds van het bestand.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

# ── Paden (pas aan als de repos elders staan) ────────────────────────────────
GIT_ROOT = Path(os.environ.get("GIT_ROOT", r"C:/GIT"))
PONSENKAART = GIT_ROOT / "ponsenkaart.nl"
INSTRUCTIEREGELS = GIT_ROOT / "instructieregels.nl"
ANNOTATIECONFORMITEIT = GIT_ROOT / "annotatieconformiteit.nl"
MER_REGISTER = GIT_ROOT / "MER-register.nl"

# Prod-Postgres-connectstring (Railway TCP-proxy). Nooit hardcoden — via env.
# OCD_PROD_URL is de naam die publish.py verwacht; dso-loader/.env kent dezelfde
# waarde als PROD_DB_URL. Zonder deze terugval draait publish.py zonder prod-DSN
# terwijl hij vlak naast een .env staat die hem heeft — dat is op 2026-08-28
# gebeurd en kostte de hele mer-register-stap.
PROD_URL = os.environ.get("OCD_PROD_URL") or os.environ.get("PROD_DB_URL")


@dataclass
class Stap:
    """Eén commando in een build- of deploy-fase."""
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    beschrijving: str = ""


@dataclass
class Site:
    naam: str
    soort: str                      # 'live' | 'baked'
    build: list[Stap] = field(default_factory=list)
    deploy: list[Stap] = field(default_factory=list)
    notitie: str = ""
    actief: bool = True             # False = bekend maar nog niet ingevuld
    preflight: "Stap | None" = None  # moet exit 0 geven, anders wordt de site overgeslagen
    # Wat er in het resultaat komt te staan als de pre-flight exit 1 geeft. Per
    # site anders: elke poort bewaakt een andere pijplijn en een andere stap.
    preflight_reden: str = "de bijbehorende pijplijn loopt achter op de data"


def _git_bash() -> str:
    """Vind Git Bash — NIET de WSL-bash uit System32 (die faalt hier)."""
    import shutil
    for c in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(c).exists():
            return c
    w = shutil.which("bash")
    if w and "System32" not in w:
        return w
    raise FileNotFoundError("Git Bash niet gevonden (vermijd WSL-bash in System32)")


def _bash(script: str, *args: str) -> list[str]:
    """Roep een bash-script aan via Git Bash op Windows."""
    return [_git_bash(), script, *args]


def sites(db_source: str) -> list[Site]:
    """Site-registry. `db_source` = 'local' (verse lokale docker) of 'prod'.

    Voor de gebakken sites bepaalt db_source waar de build zijn data leest.
    'local' is direct vers na een sync en heeft geen prod-restore nodig.
    """
    # instructieregels: build.sh gebruikt PSQL_CONN indien gezet, anders de
    # lokale docker-container. Voor db_source='prod' geven we PROD_URL mee.
    ir_env: dict[str, str] = {}
    if db_source == "prod":
        if not PROD_URL:
            print("  ! db_source=prod maar OCD_PROD_URL niet gezet", file=sys.stderr)
        else:
            ir_env["PSQL_CONN"] = PROD_URL

    # annotatieconformiteit: `collect --source ocd` leest de OCD-Postgres via
    # ODK_OCD_DB_URL. Voor prod geven we PROD_URL mee.
    ak_env: dict[str, str] = {}
    if db_source == "prod" and PROD_URL:
        ak_env["ODK_OCD_DB_URL"] = PROD_URL

    # mer-register: de twee verrijkers lezen ALTIJD prod, ook bij db_source=local.
    # `mer.project_regeling` wordt in runbook stap 6d tegen prod gebouwd en
    # `core.bronhouder` is daar geresolveerd; een lokale variant bestaat niet.
    # Vereist een open Railway-TCP-proxy (runbook stap 9 zet hem weer dicht).
    mer_env: dict[str, str] = {"PYTHONUTF8": "1"}
    if PROD_URL:
        mer_env["MER_PROD_URL"] = PROD_URL
    else:
        # Geen waarschuwing meer maar een harde stop voor déze site. Op 2026-08-28
        # stond hier een print naar stderr en draaide publish.py gewoon door; de
        # site werd toen zonder beide verrijkingen gepubliceerd en niets was
        # daaraan te zien. Een waarschuwing die je kunt missen is geen poort.
        mer_env["_ONTBREEKT"] = "OCD_PROD_URL"

    return [
        Site(
            naam="ponsenkaart",
            soort="live",
            notitie="Leest runtime uit de prod-API (/v1/ponsenkaart/*, "
                    "/v1/planvoorraad/*). Wordt vers zodra prod vers is; geen "
                    "redeploy nodig voor data. deploy.sh alleen bij code-wijziging.",
        ),
        Site(
            naam="instructieregels",
            soort="baked",
            build=[Stap(
                _bash("build/build.sh"),
                cwd=INSTRUCTIEREGELS,
                env=ir_env,
                beschrijving="build/build.sh → web/data.js (uit "
                             + ("prod" if db_source == "prod" else "lokale docker") + ")",
            )],
            deploy=[Stap(
                ["npx", "wrangler", "pages", "deploy", "web",
                 "--project-name=instructieregels-monitor", "--branch=main"],
                cwd=INSTRUCTIEREGELS,
                beschrijving="wrangler pages deploy web → Cloudflare Pages",
            )],
            # De doorwerkings-oordelen (irm.*) komen NIET uit de loader maar uit
            # een lokale Ollama-pijplijn in de instructieregels-repo. Zonder deze
            # poort bouwt publish.py de site na een sync gewoon opnieuw, met
            # oordelen van vóór die sync — en zonder één foutmelding, want de
            # tabellen zijn gevuld, alleen niet meer actueel. Zie runbook stap 6b.
            preflight=Stap(
                [sys.executable, "match/stand.py"],
                cwd=INSTRUCTIEREGELS,
                env={"PYTHONUTF8": "1"},
                beschrijving="doorwerkingsmeting bij de data? (exit 0 = ja)",
            ),
            preflight_reden="doorwerkingsmeting loopt achter op de data (runbook stap 6b)",
            notitie="Vereist CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in de "
                    "omgeving. Alternatief: `gh workflow run deploy.yml` (bouwt "
                    "echter uit Railway-prod via secrets.PSQL_CONN).",
        ),
        Site(
            naam="annotatieconformiteit",
            soort="baked",
            build=[
                Stap(
                    ["odkwaliteit", "collect", "--structuur", "--rtr"],
                    cwd=ANNOTATIECONFORMITEIT,
                    env=ak_env,
                    beschrijving="collect uit OCD-Postgres (source=ocd default; "
                                 "gemeenten uit ODK_GEMEENTE_CODES; --structuur/"
                                 "--rtr voor R6/R31/R22)",
                ),
                Stap(["odkwaliteit", "score"], cwd=ANNOTATIECONFORMITEIT,
                     beschrijving="scoring tegen de annotatierichtlijn"),
                Stap(["odkwaliteit", "export", "-f", "json"],
                     cwd=ANNOTATIECONFORMITEIT,
                     beschrijving="export → data voor de web-build"),
            ],
            deploy=[Stap(
                ["npm", "run", "deploy"],
                cwd=ANNOTATIECONFORMITEIT / "web",
                beschrijving="web/scripts/deploy.mjs → Next.js-export → Cloudflare "
                             "Pages (project annotatieconformiteit)",
            )],
            notitie="Leest OCD-Postgres via ODK_OCD_DB_URL (.env). Eigen SQLite + "
                    "scoring; deploy niet git-gekoppeld (alleen via npm run deploy). "
                    "`odkwaliteit` vereist `pip install -e .`; web `npm install`.",
        ),
        Site(
            naam="mer-register",
            soort="baked",
            build=[
                Stap(
                    [sys.executable, "harvest/export_mer_data.py"],
                    cwd=MER_REGISTER, env={"PYTHONUTF8": "1"},
                    beschrijving="harvest-store (SQLite) → web/mer-data.js",
                ),
                # De twee verrijkers schrijven IN mer-data.js en lezen PROD, niet de
                # SQLite: de regeling-koppeling zit in mer.project_regeling en de
                # canonieke BG-naam in core.bronhouder. Volgorde is dwingend —
                # export eerst, anders overschrijft hij hun velden weer.
                # resolve_bronhouder is geen optie maar een voorwaarde:
                # add_bg_uniform_to_merdata.py joint op mer.project.bronhouder_code,
                # en die kolom BESTAAT NIET tot dit script hem aanmaakt
                # (ADD COLUMN IF NOT EXISTS) en vult. Stond hier tot 2026-08-29
                # niet, waardoor de verrijker faalde met UndefinedColumn en de
                # hele mer-register-stap omviel. Idempotent en goedkoop (~10 s).
                Stap(
                    [sys.executable, "harvest/resolve_bronhouder.py"],
                    cwd=MER_REGISTER, env=mer_env,
                    beschrijving="koppel mer.event/project aan core.bronhouder "
                                 "(maakt bronhouder_code aan; voorwaarde voor de "
                                 "bevoegdGezagUniform-verrijking)",
                ),
                Stap(
                    [sys.executable, "harvest/add_regeling_to_merdata.py"],
                    cwd=MER_REGISTER, env=mer_env,
                    beschrijving="verrijk met onderliggendeRegelingen (uit prod "
                                 "mer.project_regeling)",
                ),
                Stap(
                    [sys.executable, "harvest/add_bg_uniform_to_merdata.py"],
                    cwd=MER_REGISTER, env=mer_env,
                    beschrijving="verrijk met bevoegdGezagUniform (uit prod "
                                 "core.bronhouder)",
                ),
            ],
            deploy=[Stap(
                _bash("deploy.sh"),
                cwd=MER_REGISTER,
                beschrijving="./deploy.sh → schone staging-map → Cloudflare Pages "
                             "(project mer-register)",
            )],
            # De harvest (KOOP SRU + 3.617 Commissie-pagina's) draait NIET hier maar
            # in runbook stap 6d. Zonder deze poort bakt publish.py de site na een
            # sync gewoon opnieuw uit een harvest-store van weken oud — zonder één
            # foutmelding, want de store is gevuld, alleen niet meer actueel.
            # Precies dat gebeurde tussen 21-07 en 27-08-2026.
            preflight=Stap(
                [sys.executable, "harvest/stand.py"],
                cwd=MER_REGISTER, env={"PYTHONUTF8": "1"},
                beschrijving="harvest-store bij de bron? (exit 0 = ja)",
            ),
            preflight_reden="harvest-store loopt achter op de bron (runbook stap 6d)",
            notitie="Nog gebakken: de frontend leest mer-data.js, niet /v1/mer/*. "
                    "Zodra de Pages-Function-proxy er is (deelplan E) wordt dit een "
                    "'live'-site zoals ponsenkaart en vervallen build+deploy hier. "
                    "deploy.sh haalt CLOUDFLARE_API_TOKEN zelf uit de .env van de repo; "
                    "de verrijkers hebben MER_PROD_URL nodig (Railway-TCP-proxy open).",
        ),
    ]


# ── Uitvoering ───────────────────────────────────────────────────────────────

def run_stap(s: Stap, dry: bool) -> None:
    env = {**os.environ, **s.env}
    loc = f" (cwd={s.cwd})" if s.cwd else ""
    print(f"    $ {' '.join(s.argv)}{loc}"
          + (f"  # {s.beschrijving}" if s.beschrijving else ""))
    if dry:
        return
    if s.cwd and not s.cwd.exists():
        raise FileNotFoundError(f"cwd bestaat niet: {s.cwd}")
    # Windows: npx/npm/odkwaliteit zijn .cmd/.exe — resolve via which, anders
    # vindt subprocess ze niet (WinError 2).
    import shutil
    argv = list(s.argv)
    exe = shutil.which(argv[0])
    if exe:
        argv[0] = exe
    subprocess.run(argv, cwd=s.cwd, env=env, check=True)


def run_preflight(site: Site, force: bool) -> str | None:
    """Draai de pre-flight van een site. Returns None bij ok, anders de reden.

    `force` komt van --force-preflight, NIET van --force.

    Draait OOK in dry-run: hij is read-only en het is juist de vraag die je in
    een dry-run beantwoord wilt zien.
    """
    s = site.preflight
    if s is None:
        return None
    print(f"    $ {' '.join(s.argv)} (cwd={s.cwd})  # {s.beschrijving}")
    try:
        r = subprocess.run(s.argv, cwd=s.cwd, env={**os.environ, **s.env},
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except Exception as e:
        reden = f"pre-flight niet uitvoerbaar: {e}"
        if force:
            print(f"    ! {reden} — genegeerd (--force)")
            return None
        return reden
    for regel in (r.stdout or "").splitlines():
        print(f"      {regel}")
    if r.returncode == 0:
        return None
    reden = ((r.stderr or "").strip().splitlines() or ["pre-flight faalde"])[-1] \
        if r.returncode == 2 else site.preflight_reden
    if force:
        print(f"    ! {reden} — genegeerd (--force)")
        return None
    return reden


def publiceer_site(site: Site, dry: bool, force: bool = False) -> tuple[str, str]:
    """Bouw + deploy één gebakken site. Returns (naam, 'ok'|'skip'|'fout: …')."""
    if not site.actief:
        print(f"  [{site.naam}] OVERGESLAGEN — {site.notitie}")
        return site.naam, "skip"
    if site.soort == "live":
        print(f"  [{site.naam}] live — geen build/deploy; wordt vers via prod. "
              f"{site.notitie}")
        return site.naam, "skip"
    print(f"  [{site.naam}] baked — build + deploy")
    if not site.deploy:
        print(f"    ! geen deploy-stap ingevuld — {site.notitie}")
        return site.naam, "skip"
    # Een build-stap die een env-var nodig heeft die er niet is, hoort niet te
    # starten. Zonder deze controle draaide mer-register door en publiceerde de
    # site zonder verrijking (2026-08-28) -- zichtbaar aan niets.
    ontbreekt = {st.env["_ONTBREEKT"] for st in (site.build or [])
                 if st.env and "_ONTBREEKT" in st.env}
    if ontbreekt:
        var = ", ".join(sorted(ontbreekt))
        print(f"    ! OVERGESLAGEN — {var} ontbreekt in de omgeving; de build "
              f"zou onvolledige data publiceren.", file=sys.stderr)
        return site.naam, f"overgeslagen: {var} niet gezet"

    reden = run_preflight(site, force)
    if reden:
        print(f"    ! OVERGESLAGEN — {reden}. Draai eerst de pijplijn, of "
              f"--force-preflight om toch te publiceren.", file=sys.stderr)
        return site.naam, f"overgeslagen: {reden}"
    try:
        for s in site.build:
            run_stap(s, dry)
        for s in site.deploy:
            run_stap(s, dry)
        return site.naam, "ok"
    except Exception as e:
        print(f"    ! FOUT: {e}", file=sys.stderr)
        return site.naam, f"fout: {e}"


def refresh_prod(mode: str, dry: bool) -> None:
    """Verse data naar de Railway prod-DB voor de LIVE sites.

    mode:
      none    — niets (baked sites bouwen uit lokaal; live sites blijven stale).
      delta   — AANBEVOLEN toekomst: full_sync.py incrementeel tegen prod draaien
                (goedkope registratietijdstip-delta i.p.v. 56 GB dump/restore).
                Nu nog TODO: full_sync het DB-target laten overschrijven.
      restore — bestaande zware runbook restore-dev-naar-prod.ps1 -All.
    """
    if mode == "none":
        print("  prod-refresh: overgeslagen (--prod-mode none)")
        return
    if mode == "delta":
        print("  prod-refresh: DELTA (aanbevolen) — TODO: full_sync.py met "
              "prod-DATABASE_URL + --skip-embed draaien. Vereist OCD_PROD_URL.")
        # TODO: subprocess full_sync.py met env DATABASE_URL=PROD_URL.
        return
    if mode == "restore":
        ps1 = GIT_ROOT / "OCD" / "dso-loader" / "scripts" / "restore-dev-naar-prod.ps1"
        cmd = ["powershell", "-NoProfile", "-File", str(ps1), "-All"]
        if PROD_URL:
            cmd += ["-ProdUrl", PROD_URL]
        print(f"    $ {' '.join(cmd[:5])} …  # zware destructieve restore")
        if not dry:
            subprocess.run(cmd, check=True)


def _sync_status() -> str | None:
    """Status van de laatste sync-run uit audit.sync_run (None als onbekend)."""
    try:
        import psycopg
        url = os.environ.get("DATABASE_URL",
                             "postgresql://postgres:postgres@localhost:5434/dso")
        with psycopg.connect(url) as c, c.cursor() as cur:
            cur.execute("SELECT opmerking FROM audit.sync_run "
                        "ORDER BY gestart_op DESC LIMIT 1")
            row = cur.fetchone()
            return (row[0] or "") if row else None
    except Exception as e:
        print(f"  (sync-status niet te bepalen: {e})", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="daadwerkelijk draaien (default = dry-run)")
    ap.add_argument("--only", default=None,
                    help="alleen deze site "
                         "(ponsenkaart|instructieregels|annotatieconformiteit)")
    ap.add_argument("--db-source", choices=["local", "prod"], default="local",
                    help="databron voor de gebakken builds (default local = vers na sync)")
    ap.add_argument("--prod-mode", choices=["none", "delta", "restore"], default="none",
                    help="prod verversen voor de live sites (default none)")
    ap.add_argument("--force", action="store_true",
                    help="publiceer ook als de laatste sync-run geen 'ok' was")
    # BEWUST een aparte vlag. Als --force beide poorten zou dekken, schakelt
    # iedereen die langs een rode sync-status moet ook de doorwerkingspoort uit
    # zonder dat te bedoelen — en de sync-status staat vaker rood dan je denkt.
    ap.add_argument("--force-preflight", action="store_true",
                    help="publiceer ook als een site-pre-flight faalt "
                         "(bv. doorwerkingsmeting die achterloopt)")
    args = ap.parse_args()
    dry = not args.execute

    print("=" * 70)
    print(f"publish.py — {'DRY-RUN (niets wordt uitgevoerd)' if dry else 'EXECUTE'}"
          f" | db-source={args.db_source} | prod-mode={args.prod_mode}")
    print("=" * 70)

    # Poort: alleen na een geslaagde sync (tenzij --force). De sync zet
    # opmerking op "<n> fouten" — parse het getal (niet op de substring "fout"
    # matchen, want "0 fouten" bevat die ook).
    import re
    status = _sync_status()
    m = re.search(r"(\d+)\s*fout", status or "")
    n_fout = int(m.group(1)) if m else 0
    schoon = status is not None and n_fout == 0 and "afgebroken" not in (status or "").lower()
    if schoon:
        print(f"  poort: laatste sync-run OK ({status!r})")
    elif args.force:
        print(f"  poort: sync-status {status!r} — genegeerd (--force)")
    else:
        print(f"  poort: laatste sync-run niet schoon ({status!r}). "
              f"Gebruik --force om toch te publiceren.", file=sys.stderr)
        return 2

    # Spoor 1 — prod verversen (live sites).
    print("\n[1] prod-refresh (live sites: ponsenkaart, prod-viewer, bot)")
    refresh_prod(args.prod_mode, dry)

    # Spoor 2 — gebakken sites herbouwen + deployen.
    print("\n[2] gebakken sites herbouwen + deployen")
    resultaten: list[tuple[str, str]] = []
    for site in sites(args.db_source):
        if args.only and site.naam.lower() != args.only.lower():
            continue
        resultaten.append(publiceer_site(site, dry, args.force_preflight))

    print("\n" + "=" * 70)
    print("resultaat:")
    for naam, status in resultaten:
        print(f"  {naam:22} {status}")
    # "overgeslagen" telt mee als niet-nul: een site die vanwege een gefaalde
    # pre-flight niet gepubliceerd is, mag een aanroepend script niet als succes
    # binnenkrijgen. ("skip" voor live sites is iets anders — dat is de normale
    # gang van zaken en blijft 0.)
    fouten = [n for n, s in resultaten if s.startswith(("fout", "overgeslagen"))]
    print("=" * 70)
    if dry:
        print("DRY-RUN — draai met --execute om echt te publiceren.")
    return 1 if fouten else 0


if __name__ == "__main__":
    sys.exit(main())
