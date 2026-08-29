"""Repliceer de p2p-rijen van een sync-run van lokaal naar productie.

Waarom dit script bestaat
-------------------------
Sinds 2026-08-08 (gebruiker-keuze) draait productie geen loaders meer. De
harvest gebeurt één keer, op de lokale werkbank; prod krijgt de rijen die daar
al liggen. Zie `docs/synchronisatie-runbook.md` §Stap 3. Dat vervangt
`full_sync.py --target prod`, waarbij beide databases onafhankelijk van de DSO
laadden en dus uiteen kónden lopen.

Wat wél en niet over de lijn gaat
---------------------------------
Gemeten 2026-08-07: `p2p` is lokaal 24 GB, waarvan `locatie_subdiv` 12 GB en
`locatie` 6,7 GB. De tien regelingen van die sync besloegen 14.100
`tekst_element` en 6.489 `juridische_regel`. Brondata kopiëren is dus goedkoop;
afgeleide objecten meesturen zou het duur maken. Daarom:

  kopiëren   de tabellen hieronder, gefilterd op de expressie-set
  herbouwen  `locatie_subdiv` (ST_Subdivide) en de MV's — apart, ná dit script

Hoe de scope wordt bepaald
--------------------------
De dimensietabellen (`locatie`, `activiteit`, `norm`, `kaart`, `hoofdlijn`,
`tekstdeel`, `pons`) hebben géén regeling- of bronhouderkolom: ze hangen alleen
via junctions aan een regeling, en worden tussen regelingen gedeeld. De scope
volgt daarom de FK-graaf vanaf de nieuwe expressies, niet een aanname over
"hoort bij één regeling". `tekstdeel` hangt via `divisie_wid = tekst_element.wid`
(geen FK; zie ow_loader.py:680).

Bestaande rijen worden **bijgewerkt**, niet overgeslagen (ON CONFLICT (pk) DO
UPDATE). Dat is geen detail maar de kern van het geval "nieuwe versie van een
bestaand plan": de IMOW-objecten houden dan hun `identificatie` — die is de
primaire sleutel — maar krijgen een nieuwe `regeling_expression`. Met DO NOTHING
blijven ze op prod naar de óude expressie wijzen, en toont de viewer voor het
vernieuwde plan bijna niets. Gemeten bij de eerste poging op 2026-08-08: lokaal
6.489 juridische regels voor de tien expressies, op prod 244.

Lokaal is dus de waarheid. Rijen die prod wél heeft en lokaal niet, blijven
staan — dit script verwijdert niets.

Kolomvolgorde wordt aan beide kanten vergeleken vóór er iets gebeurt. Bij drift
stopt het script: een COPY op verschoven kolommen schrijft stilletjes onzin.

Draaien:  python scripts/repliceer_p2p_naar_prod.py [--sinds <ISO>] [--ja]
Zonder --ja is het een droogloop: hij toont per tabel hoeveel rijen in scope
zijn, hoeveel prod er al heeft en hoeveel er zouden worden gekopieerd.
"""

import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LOKAAL = os.environ.get("OCD_DB_URL", "postgresql://postgres:postgres@localhost:5434/dso")
PROD = os.environ["PROD_DB_URL"]


# De voortgangsregels bevatten een → (U+2192). Op een cp1252-console gooit dat
# een UnicodeEncodeError, en dan sterft het script ná de COPY maar vóór de merge:
# twintig minuten kopieerwerk weg op een pijltje. Gebeurd 2026-08-22, stap 3 van
# de sync. Zes zusterscripts in deze map deden dit al; dit script niet.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def log(*a):
    print(*a, flush=True)


# ── scope ────────────────────────────────────────────────────────────
# Bouwt de id-sets als TEMP-tabellen op de lokale kant. Volgorde telt: elke
# set leunt op de vorige.

# Eerst apart: dit is het enige statement met een parameter, en psycopg3 staat
# geen meerdere commando's in één prepared statement toe.
SCOPE_EXPR_SQL = """
CREATE TEMP TABLE scope_expr ON COMMIT DROP AS
    SELECT frbr_expression FROM p2p.regeling_load WHERE geladen_op >= %(sinds)s
"""

SCOPE_SQL = """
CREATE TEMP TABLE scope_te ON COMMIT DROP AS
    SELECT id, wid FROM p2p.tekst_element
     WHERE regeling_expression IN (SELECT frbr_expression FROM scope_expr);

CREATE TEMP TABLE scope_jr ON COMMIT DROP AS
    SELECT identificatie FROM p2p.juridische_regel
     WHERE regeling_expression IN (SELECT frbr_expression FROM scope_expr);

CREATE TEMP TABLE scope_gio ON COMMIT DROP AS
    SELECT frbr_expression FROM p2p.geo_informatieobject
     WHERE regeling_expression IN (SELECT frbr_expression FROM scope_expr);

CREATE TEMP TABLE scope_besluit ON COMMIT DROP AS
    SELECT DISTINCT besluit_expression FROM p2p.besluit_regeling
     WHERE regeling_expression IN (SELECT frbr_expression FROM scope_expr);

-- activiteiten: via de locatieaanduiding, plus de hele bovenliggende-keten,
-- anders faalt de self-FK aan de prod-kant.
CREATE TEMP TABLE scope_act ON COMMIT DROP AS
WITH RECURSIVE direct AS (
    SELECT DISTINCT a.identificatie, a.bovenliggende
      FROM p2p.activiteit a
      JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.identificatie
     WHERE ala.juridische_regel_id IN (SELECT identificatie FROM scope_jr)
), keten AS (
    SELECT identificatie, bovenliggende, 0 AS diepte FROM direct
    UNION
    SELECT p.identificatie, p.bovenliggende, k.diepte + 1
      FROM keten k JOIN p2p.activiteit p ON p.identificatie = k.bovenliggende
     WHERE k.diepte < 20
)
SELECT identificatie, max(diepte) AS diepte FROM keten GROUP BY identificatie;

CREATE TEMP TABLE scope_ga ON COMMIT DROP AS
    SELECT DISTINCT gebiedsaanwijzing_id AS identificatie
      FROM p2p.juridische_regel_gebiedsaanwijzing
     WHERE juridische_regel_id IN (SELECT identificatie FROM scope_jr);

CREATE TEMP TABLE scope_norm ON COMMIT DROP AS
    SELECT DISTINCT norm_id AS identificatie
      FROM p2p.juridische_regel_norm
     WHERE juridische_regel_id IN (SELECT identificatie FROM scope_jr);

-- locaties: alles wat vanuit de scope naar een locatie wijst.
CREATE TEMP TABLE scope_loc ON COMMIT DROP AS
    SELECT DISTINCT locatie_id AS identificatie FROM (
        SELECT locatie_id FROM p2p.activiteit_locatieaanduiding
         WHERE juridische_regel_id IN (SELECT identificatie FROM scope_jr)
        UNION
        SELECT locatie_id FROM p2p.gebiedsaanwijzing
         WHERE identificatie IN (SELECT identificatie FROM scope_ga)
        UNION
        SELECT locatie_id FROM p2p.normwaarde
         WHERE norm_id IN (SELECT identificatie FROM scope_norm)
    ) x WHERE locatie_id IS NOT NULL;

-- groepslocaties: een groep in scope trekt zijn leden mee (FK beide kanten).
INSERT INTO scope_loc
    SELECT DISTINCT lg.lid_identificatie FROM p2p.locatiegroep_lid lg
     WHERE lg.groep_identificatie IN (SELECT identificatie FROM scope_loc)
       AND lg.lid_identificatie NOT IN (SELECT identificatie FROM scope_loc);

-- tekstdeel hangt via de locatie in scope, NIET via divisie_wid. Die kolom bevat
-- een IMOW-identificatie (nl.imow-gm0363.divisietekst.<uuid>) terwijl
-- tekst_element.wid een STOP-wId is (mnre1034_1-0__chp_3__...): twee
-- naamruimten die per definitie niet joinen — gemeten 0 van 27.223 treffers.
-- (Diezelfde join staat in ow_loader.py:680 voor regeling_load.n_locatie; die
-- teller staat daardoor landelijk op NULL/0. Zie sync-2026-08-07.md.)
-- Deze scope kan iets rúímer zijn dan de regeling: een tekstdeel van een andere
-- regeling op dezelfde locatie komt mee. Met ON CONFLICT DO NOTHING is dat
-- ruis, geen schade; te krap zou wél een gat op prod achterlaten.
CREATE TEMP TABLE scope_td ON COMMIT DROP AS
    SELECT identificatie, locatie_id FROM p2p.tekstdeel
     WHERE locatie_id IN (SELECT identificatie FROM scope_loc);

-- Gebiedsaanwijzingen komen langs twéé routes binnen: via de juridische regel
-- (scope_ga hierboven) en via het tekstdeel. Die tweede route kwam er op
-- 2026-08-09 bij met de reparatie van G-124 en stond hier niet, waardoor na de
-- sync van 21-08 38 gebiedsaanwijzingen op prod ontbraken (vault G-130).
-- Het is geen randgeval: landelijk hangen 2.049 gebiedsaanwijzingen aan géén
-- enkele juridische regel en dus uitsluitend aan een tekstdeel.
-- Eén doorgang volstaat: de nieuwe gebiedsaanwijzingen kunnen scope_td niet
-- verder verbreden, want tekstdelen hangen aan locaties, niet aan
-- gebiedsaanwijzingen.
INSERT INTO scope_ga
    SELECT DISTINCT tga.gebiedsaanwijzing_id
      FROM p2p.tekstdeel_gebiedsaanwijzing tga
     WHERE tga.tekstdeel_id IN (SELECT identificatie FROM scope_td)
       AND tga.gebiedsaanwijzing_id NOT IN (SELECT identificatie FROM scope_ga);

-- die extra gebiedsaanwijzingen dragen een locatie-FK die mee moet, anders
-- faalt de INSERT aan de prod-kant.
INSERT INTO scope_loc
    SELECT DISTINCT g.locatie_id FROM p2p.gebiedsaanwijzing g
     WHERE g.identificatie IN (SELECT identificatie FROM scope_ga)
       AND g.locatie_id IS NOT NULL
       AND g.locatie_id NOT IN (SELECT identificatie FROM scope_loc);

-- en is zo'n locatie een groep, dan moeten zijn leden er weer bij (zelfde
-- reden als de groepsuitbreiding hierboven; die draaide vóór deze aanvulling).
INSERT INTO scope_loc
    SELECT DISTINCT lg.lid_identificatie FROM p2p.locatiegroep_lid lg
     WHERE lg.groep_identificatie IN (SELECT identificatie FROM scope_loc)
       AND lg.lid_identificatie NOT IN (SELECT identificatie FROM scope_loc);

CREATE TEMP TABLE scope_kaart ON COMMIT DROP AS
    SELECT DISTINCT kaart_id AS identificatie FROM p2p.kaartlaag
     WHERE activiteit_id IN (SELECT identificatie FROM scope_act)
        OR gebiedsaanwijzing_id IN (SELECT identificatie FROM scope_ga)
        OR norm_id IN (SELECT identificatie FROM scope_norm);

CREATE TEMP TABLE scope_hoofdlijn ON COMMIT DROP AS
    SELECT DISTINCT hoofdlijn_id AS identificatie FROM p2p.tekstdeel_hoofdlijn
     WHERE tekstdeel_id IN (SELECT identificatie FROM scope_td);
"""


# ── kopieerplan ──────────────────────────────────────────────────────
# (tabel, SELECT-filter). FK-volgorde: ouders vóór kinderen. De self-FK's
# (activiteit.bovenliggende, tekst_element.parent_id) worden op diepte
# gesorteerd zodat een ouder nooit ná zijn kind wordt ingevoegd.

# Zelfverwijzende tabellen: de ouder moet vóór het kind worden ingevoegd, anders
# faalt de FK. De diepte reist mee als `_d` en bepaalt de INSERT-volgorde.
# activiteit: scope_act telt oplopend ríchting de wortel, dus aflopend invoegen.
# tekst_element: diepte 0 is de wortel, dus oplopend.
ORDERING = {
    "p2p.activiteit": "_d DESC",
    "p2p.tekst_element": "_d ASC",
}

PLAN = [
    ("p2p.locatie",
     "SELECT t.* FROM p2p.locatie t JOIN scope_loc s ON s.identificatie = t.identificatie"),

    ("p2p.activiteit",
     "SELECT t.*, s.diepte AS _d FROM p2p.activiteit t "
     "JOIN scope_act s ON s.identificatie = t.identificatie"),

    ("p2p.norm",
     "SELECT t.* FROM p2p.norm t JOIN scope_norm s ON s.identificatie = t.identificatie"),

    ("p2p.kaart",
     "SELECT t.* FROM p2p.kaart t JOIN scope_kaart s ON s.identificatie = t.identificatie"),

    ("p2p.hoofdlijn",
     "SELECT t.* FROM p2p.hoofdlijn t JOIN scope_hoofdlijn s ON s.identificatie = t.identificatie"),

    ("p2p.besluit",
     "SELECT t.* FROM p2p.besluit t JOIN scope_besluit s ON s.besluit_expression = t.frbr_expression"),

    ("p2p.gebiedsaanwijzing",
     "SELECT t.* FROM p2p.gebiedsaanwijzing t JOIN scope_ga s ON s.identificatie = t.identificatie"),

    ("p2p.regeling",
     "SELECT t.* FROM p2p.regeling t JOIN scope_expr s ON s.frbr_expression = t.frbr_expression"),

    ("p2p.tekst_element",
     "WITH RECURSIVE d AS ("
     "  SELECT id, parent_id, 0 AS diepte FROM p2p.tekst_element"
     "   WHERE id IN (SELECT id FROM scope_te) AND parent_id IS NULL"
     "  UNION ALL"
     "  SELECT c.id, c.parent_id, d.diepte + 1 FROM p2p.tekst_element c"
     "    JOIN d ON c.parent_id = d.id WHERE d.diepte < 40)"
     " SELECT t.*, d.diepte AS _d FROM p2p.tekst_element t JOIN d ON d.id = t.id"),

    ("p2p.regeling_load",
     "SELECT t.* FROM p2p.regeling_load t JOIN scope_expr s ON s.frbr_expression = t.frbr_expression"),

    ("p2p.geo_informatieobject",
     "SELECT t.* FROM p2p.geo_informatieobject t JOIN scope_gio s ON s.frbr_expression = t.frbr_expression"),

    ("p2p.juridische_regel",
     "SELECT t.* FROM p2p.juridische_regel t JOIN scope_jr s ON s.identificatie = t.identificatie"),

    ("p2p.tekstdeel",
     "SELECT t.* FROM p2p.tekstdeel t JOIN scope_td s ON s.identificatie = t.identificatie"),

    # junctions en bladeren
    ("p2p.besluit_regeling",
     "SELECT t.* FROM p2p.besluit_regeling t "
     "WHERE t.regeling_expression IN (SELECT frbr_expression FROM scope_expr)"),

    ("p2p.procedurestap",
     "SELECT t.* FROM p2p.procedurestap t "
     "WHERE t.besluit_expression IN (SELECT besluit_expression FROM scope_besluit)"),

    ("p2p.activiteit_locatieaanduiding",
     "SELECT t.* FROM p2p.activiteit_locatieaanduiding t "
     "WHERE t.juridische_regel_id IN (SELECT identificatie FROM scope_jr)"),

    ("p2p.juridische_regel_gebiedsaanwijzing",
     "SELECT t.* FROM p2p.juridische_regel_gebiedsaanwijzing t "
     "WHERE t.juridische_regel_id IN (SELECT identificatie FROM scope_jr)"),

    ("p2p.juridische_regel_norm",
     "SELECT t.* FROM p2p.juridische_regel_norm t "
     "WHERE t.juridische_regel_id IN (SELECT identificatie FROM scope_jr)"),

    ("p2p.normwaarde",
     "SELECT t.* FROM p2p.normwaarde t "
     "WHERE t.norm_id IN (SELECT identificatie FROM scope_norm) "
     "  AND t.locatie_id IN (SELECT identificatie FROM scope_loc)"),

    ("p2p.kaartlaag",
     "SELECT t.* FROM p2p.kaartlaag t "
     "WHERE t.kaart_id IN (SELECT identificatie FROM scope_kaart)"),

    ("p2p.tekstdeel_hoofdlijn",
     "SELECT t.* FROM p2p.tekstdeel_hoofdlijn t "
     "WHERE t.tekstdeel_id IN (SELECT identificatie FROM scope_td)"),

    # Stond hier tot 2026-08-28 helemaal niet, terwijl de tabel sinds de
    # G-124-reparatie gevuld wordt: 7.118 rijen lokaal tegen 6.919 op prod.
    ("p2p.tekstdeel_gebiedsaanwijzing",
     "SELECT t.* FROM p2p.tekstdeel_gebiedsaanwijzing t "
     "WHERE t.tekstdeel_id IN (SELECT identificatie FROM scope_td) "
     "  AND t.gebiedsaanwijzing_id IN (SELECT identificatie FROM scope_ga)"),

    ("p2p.pons",
     "SELECT t.* FROM p2p.pons t WHERE t.locatie_id IN (SELECT identificatie FROM scope_loc)"),

    ("p2p.locatiegroep_lid",
     "SELECT t.* FROM p2p.locatiegroep_lid t "
     "WHERE t.groep_identificatie IN (SELECT identificatie FROM scope_loc) "
     "  AND t.lid_identificatie IN (SELECT identificatie FROM scope_loc)"),

    ("p2p.locatie_basisgeo",
     "SELECT t.* FROM p2p.locatie_basisgeo t "
     "WHERE t.locatie_id IN (SELECT identificatie FROM scope_loc)"),

    ("p2p.gio_basisgeo",
     "SELECT t.* FROM p2p.gio_basisgeo t "
     "WHERE t.gio_frbr IN (SELECT frbr_expression FROM scope_gio)"),

    ("p2p.juridische_borging",
     "SELECT t.* FROM p2p.juridische_borging t "
     "WHERE t.gio_expression IN (SELECT frbr_expression FROM scope_gio)"),

    ("p2p.tekst_inline_referentie",
     "SELECT t.* FROM p2p.tekst_inline_referentie t "
     "WHERE t.tekst_element_id IN (SELECT id FROM scope_te)"),
]


def kolommen(cur, tabel: str) -> list[str]:
    """Kolommen die we kopiëren: alles behalve GENERATED-kolommen.

    `p2p.tekst_element.inhoud_plain` is een stored generated column; die
    invoegen is een harde fout (en zinloos — prod berekent hem zelf).
    Identity-kolommen blijven er juist wél in, zie identity_kolommen().
    """
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND is_generated <> 'ALWAYS' "
        "ORDER BY ordinal_position",
        (schema, naam))
    return [r[0] for r in cur.fetchall()]


def pk_kolommen(cur, tabel: str) -> list[str]:
    """Primaire-sleutelkolommen — het conflict-target voor de upsert."""
    schema, naam = tabel.split(".")
    cur.execute("""
        SELECT a.attname
          FROM pg_constraint pc
          JOIN pg_class c ON c.oid = pc.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN LATERAL unnest(pc.conkey) WITH ORDINALITY k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
         WHERE pc.contype = 'p' AND n.nspname = %s AND c.relname = %s
         ORDER BY k.ord""", (schema, naam))
    return [r[0] for r in cur.fetchall()]


def identity_kolommen(cur, tabel: str) -> list[str]:
    """Kolommen met een eigen waardegenerator (identity of serial).

    We nemen de lokale id's mee in plaats van prod nieuwe te laten uitdelen:
    `tekst_inline_referentie.tekst_element_id` en `v2a.tekst_embedding.
    tekst_element_id` verwijzen ernaar, dus hernummeren zou die koppelingen
    stilzwijgend naar andere tekst laten wijzen. GENERATED ALWAYS eist daarvoor
    OVERRIDING SYSTEM VALUE, en achteraf moet de sequence mee.
    """
    schema, naam = tabel.split(".")
    cur.execute(
        "SELECT column_name, is_identity, identity_generation, column_default "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, naam))
    uit = []
    for kol, is_id, gen, default in cur.fetchall():
        if is_id == "YES" or (default or "").startswith("nextval("):
            uit.append((kol, gen == "ALWAYS"))
    return uit


def controleer_schema(lc, pc) -> None:
    """Zelfde kolommen, zelfde volgorde — anders schrijft COPY onzin."""
    fout = []
    for tabel, _ in PLAN:
        kl, kp = kolommen(lc, tabel), kolommen(pc, tabel)
        if kl != kp:
            fout.append(f"  {tabel}\n    lokaal: {kl}\n    prod  : {kp}")
    if fout:
        sys.exit("STOP: kolomdrift tussen lokaal en prod:\n" + "\n".join(fout))
    log(f"  schema-check ok — {len(PLAN)} tabellen, identieke kolomvolgorde")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sinds", help="ISO-tijdstip; default = start van de laatste geslaagde lokale sync")
    ap.add_argument("--expressies", metavar="BESTAND",
                    help="bestand met één frbr_expression per regel; overschrijft --sinds. "
                         "Voor reparaties die bestaande regelingen raken in plaats van "
                         "nieuw geladene — bv. het GIO-koppelingsherstel (G-119), waarbij "
                         "geo_informatieobject en tekst_inline_referentie van oude "
                         "expressies zijn bijgewerkt.")
    ap.add_argument("--ja", action="store_true", help="echt kopiëren (anders droogloop)")
    args = ap.parse_args()

    lconn = psycopg.connect(LOKAAL)
    pconn = psycopg.connect(PROD, connect_timeout=30)
    lc, pc = lconn.cursor(), pconn.cursor()

    # De Railway-container heeft een kleine /dev/shm; parallelle workers laten
    # grote operaties falen met "could not resize shared memory segment".
    # get_conn() doet dit bij een prod-DSN automatisch, maar dit script verbindt
    # rechtstreeks. Gemeten 2026-08-08 op core.mv_bronhouder_health: mét
    # parallellisme een harde fout, zonder 16,2 s.
    pc.execute("SET max_parallel_workers_per_gather = 0")
    pc.execute("SET max_parallel_maintenance_workers = 0")

    expressies: list[str] | None = None
    if args.expressies:
        with open(args.expressies, encoding="utf-8") as f:
            expressies = [r.strip() for r in f if r.strip()]
        log(f"Scope: {len(expressies)} expressies uit {args.expressies}")
        sinds = None
    else:
        sinds = args.sinds
        if not sinds:
            lc.execute("SELECT max(gestart_op) FROM audit.sync_run "
                       "WHERE klaar_op IS NOT NULL AND coalesce(opmerking,'') NOT ILIKE '%%afgebroken%%'")
            sinds = lc.fetchone()[0]
        log(f"Scope: regelingen geladen sinds {sinds}")

    controleer_schema(lc, pc)

    if expressies is not None:
        lc.execute("CREATE TEMP TABLE scope_expr ON COMMIT DROP AS "
                   "SELECT unnest(%s::text[]) AS frbr_expression", (expressies,))
    else:
        lc.execute(SCOPE_EXPR_SQL, {"sinds": sinds})
    lc.execute(SCOPE_SQL)
    lc.execute("SELECT count(*) FROM scope_expr")
    n_expr = lc.fetchone()[0]
    if n_expr == 0:
        log("Geen expressies in scope — niets te doen.")
        return
    lc.execute("SELECT frbr_expression FROM scope_expr ORDER BY 1")
    for (e,) in lc.fetchall():
        log(f"    {e}")
    log(f"  {n_expr} expressies in scope")

    totaal_nieuw = 0
    for tabel, select in PLAN:
        t0 = time.time()
        lc.execute(f"SELECT count(*) FROM ({select}) q")
        n_scope = lc.fetchone()[0]
        if n_scope == 0:
            log(f"  {tabel:42} 0 in scope")
            continue

        if not args.ja:
            log(f"  {tabel:42} {n_scope:>9,} in scope")
            totaal_nieuw += n_scope
            continue

        kol_lijst = kolommen(lc, tabel)
        kols = ", ".join(f'"{k}"' for k in kol_lijst)
        ord_expr = ORDERING.get(tabel)
        stg = "stg_" + tabel.split(".")[1]

        # Staging expliciet opbouwen uit de kolomlijst — niet LIKE, want dat
        # erft de GENERATED-kolommen en die kun je niet vullen.
        extra_kol = ", 0::int AS _d" if ord_expr else ""
        pc.execute(f"CREATE TEMP TABLE {stg} ON COMMIT DROP AS "
                   f"SELECT {kols}{extra_kol} FROM {tabel} WITH NO DATA")

        bron = f"SELECT {', '.join(f'q.\"{k}\"' for k in kol_lijst)}" \
               f"{', q._d' if ord_expr else ''} FROM ({select}) q"
        doel_kols = kols + (", _d" if ord_expr else "")

        # FORMAT TEXT, niet BINARY: lokaal is PG 16.9/PostGIS 3.5 en prod
        # PG 17.10/PostGIS 3.7. Binary COPY is per type versiegevoelig; bij dit
        # volume (tienduizenden rijen) is de winst het risico niet waard.
        with lc.copy(f"COPY ({bron}) TO STDOUT (FORMAT TEXT)") as uit:
            with pc.copy(f"COPY {stg} ({doel_kols}) FROM STDIN (FORMAT TEXT)") as inn:
                for blok in uit:
                    inn.write(blok)
        idents = identity_kolommen(pc, tabel)
        overriding = " OVERRIDING SYSTEM VALUE" if any(altijd for _, altijd in idents) else ""
        order_by = f" ORDER BY {ord_expr}" if ord_expr else ""

        pk = pk_kolommen(pc, tabel)
        niet_pk = [k for k in kol_lijst if k not in pk]
        if pk and niet_pk:
            conflict = (f"ON CONFLICT ({', '.join(f'\"{k}\"' for k in pk)}) DO UPDATE SET "
                        + ", ".join(f'"{k}"=EXCLUDED."{k}"' for k in niet_pk))
        else:
            conflict = "ON CONFLICT DO NOTHING"

        # xmax = 0 onderscheidt een echte INSERT van een DO UPDATE, zodat het
        # rapport laat zien wat nieuw is en wat bijgewerkt (zie gio_zip.py:345).
        pc.execute(f"INSERT INTO {tabel} ({kols}){overriding} "
                   f"SELECT {kols} FROM {stg}{order_by} "
                   f"{conflict} RETURNING (xmax = 0)")
        uitslag = pc.fetchall()
        n_ingevoegd = sum(1 for (nieuw,) in uitslag if nieuw)
        n_bijgewerkt = len(uitslag) - n_ingevoegd
        for kol, _ in idents:
            # sequence meeschuiven, anders botst de eerstvolgende insert op prod
            pc.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                f"  coalesce((SELECT max({kol}) FROM {tabel}), 1))", (tabel, kol))
        totaal_nieuw += n_ingevoegd
        log(f"  {tabel:42} {n_scope:>9,} in scope → {n_ingevoegd:>7,} nieuw, "
            f"{n_bijgewerkt:>7,} bijgewerkt ({time.time() - t0:.1f}s)")

    if not args.ja:
        log(f"\nDROOGLOOP — {totaal_nieuw:,} rijen zouden worden aangeboden. "
            f"Draai opnieuw met --ja.")
        return

    # inactief-vlaggen gelijktrekken: de enige plek waar prod wél wordt
    # overschreven, want stap 2 markeert verdrongen versies en die markering
    # moet productie halen (anders staan oude en nieuwe versie naast elkaar).
    lc.execute("SELECT frbr_expression, inactief, datum_inactief, reden_inactief "
               "FROM p2p.regeling WHERE inactief")
    rijen = lc.fetchall()
    n_upd = 0
    for expr, inact, dat, reden in rijen:
        pc.execute("UPDATE p2p.regeling SET inactief=%s, datum_inactief=%s, reden_inactief=%s "
                   "WHERE frbr_expression=%s AND inactief IS DISTINCT FROM %s",
                   (inact, dat, reden, expr, inact))
        n_upd += pc.rowcount
    log(f"  inactief-vlaggen bijgewerkt: {n_upd} (van {len(rijen)} lokaal inactief)")

    pconn.commit()
    log(f"\nKlaar — {totaal_nieuw:,} rijen ingevoegd.")
    log("Nu nog op prod: locatie_subdiv herbouwen voor de geraakte bronhouders, "
        "daarna refresh_drieslag.py en de health-MV's. Zie runbook §Stap 3.")


if __name__ == "__main__":
    main()
