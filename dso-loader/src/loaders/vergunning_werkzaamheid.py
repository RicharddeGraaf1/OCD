"""Koppel een omgevingsvergunning aan een Werkzaamheid, en daarmee aan de regels.

De keten staat in docs/vergunning-regelkoppeling-plan.md. Alleen de eerste
sprong — tekst -> werkzaamheid — moet geraden worden; alles daarna is door
bronhouders gecureerd en loopt via de view vth.v_vergunning_regeltekst.

Deze loader schrijft uitsluitend die eerste sprong weg, mét zijn bewijs, in
vth.vergunning_werkzaamheid.

Vier ingrepen ten opzichte van het eerste prototype, elk op een gemeten fout:

  1. ADRES ERUIT. Straatnaam, woonplaats, geometrielabel en de naam van het
     bevoegd gezag worden uit de matchtekst geknipt. Twee van 25 handmatig
     bekeken fouten kwamen hiervandaan: 's-Gravenhage matchte op het trefwoord
     'haag' (Boom of beplanting weghalen), "Nieuwe Wetering 26A" op 'wetering'
     (Graven in bodem). Het adres staat al in eigen kolommen, dus we hoeven het
     niet te herkennen — we trekken het af.

  2. DOCUMENTFREQUENTIE. Een frase die in meer dan MAX_DF_FRACTIE van de
     vergunningteksten voorkomt draagt geen informatie, hoe zeldzaam ze ook is
     binnen de werkzaamhedenlijst. Zonder dit filter trok het trefwoord 'meer'
     — een meer, het water — 24% van alle vergunningen naar "Zwembad of andere
     zwemlocatie exploiteren".

  3. DE KOOP-RUBRIEK ALS WEGING. Gemeten: 'kappen' komt 40/40 uit op een
     kapactiviteit en 'reclame' 6/6 op een reclameactiviteit, maar bij 'bouwen'
     landt 14% op een milieubelastende activiteit. Een straf in de
     rangschikking, geen hek — een monument mag verbouwd worden, dus
     'bouwen' -> monumentenactiviteit is legitiem.

  4. HET WERKWOORD TELT MEE. Werkzaamheidsnamen zijn bijna allemaal
     `<object> <handeling>`. Zonder deze weging scoort "het verbouwen van een
     kinderdagverblijf" op het zelfstandig naamwoord en komt het uit op
     "Kantoor of ander verblijfsgebouw" (exploiteren) in plaats van op
     "Bestaand gebouw verbouwen".

Wat hier bewust NIET in zit: embeddings als herrangschikker. Die horen pas
gemeten te worden als 1-4 liggen, anders weet je niet wat ze toevoegen.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from psycopg.rows import dict_row

from src.db import get_conn

log = logging.getLogger(__name__)

METHODE = "lexicaal"
METHODE_VERSIE = "lex-v2"

MAX_URNS_PER_FRASE = 2      # frase mag bij hoogstens zoveel werkzaamheden horen
MAX_DF_FRACTIE = 0.10       # ... en in hoogstens dit deel van de teksten staan
MAX_RANG = 10               # afkappen bij schrijven maakt de top-N-knop onomkeerbaar
RUBRIEK_STRAF = 150.0       # strafpunten als de rubriek de activiteit tegenspreekt
HANDELING_BONUS = 60.0      # bonus als de handeling uit de naam in de tekst staat

_HANDELINGEN = {
    "plaatsen", "vervangen", "veranderen", "verbouwen", "bouwen", "slopen",
    "aanleggen", "weghalen", "verwijderen", "kappen", "vellen", "onderhouden",
    "uitvoeren", "gebruiken", "exploiteren", "oprichten", "uitbreiden",
    "wijzigen", "graven", "dempen", "leggen", "verplaatsen", "repareren",
    "aanmaken", "opslaan", "lozen", "houden", "aanpassen", "isoleren",
}

# KOOP-rubriek -> activiteitengroepen die daarbij passen.
_RUBRIEK_GROEPEN: dict[str, set[str]] = {
    "bouwen": {"bouwactiviteit ruimtelijk", "bouwactiviteit technisch",
               "monumentenactiviteit", "cultureel-erfgoedactiviteit"},
    "kappen": {"kapactiviteit"},
    "slopen": {"sloopactiviteit", "bouwactiviteit ruimtelijk"},
    "reclame": {"reclameactiviteit"},
    "uitweg en inrit": {"uitritactiviteit"},
    "milieu": {"milieubelastende activiteit", "lozingsactiviteit"},
    "natuur": {"natuuractiviteit", "kapactiviteit", "flora- en fauna-activiteit"},
    "brandveilig gebruik": {"brandveilig gebruik", "bouwactiviteit technisch"},
    # 'ruimtelijke ordening' is een verzamelbak (vooral BOPA) en weegt niet mee.
}

# De matchtekst: titel + beschrijving + het begin van de bodytekst, met de
# adresvelden eruit geknipt.
_TEKST_SQL = """
lower(regexp_replace(
  regexp_replace(
    coalesce(v.titel,'') || ' ' || coalesce(v.beschrijving,'') || ' ' ||
    left(coalesce(v.inhoud_tekst,''), 4000),
    coalesce(nullif(concat_ws('|',
       nullif(regexp_replace(coalesce(v.straatnaam,''),   '[^[:alnum:] ]', '', 'g'), ''),
       nullif(regexp_replace(coalesce(v.woonplaats,''),   '[^[:alnum:] ]', '', 'g'), ''),
       nullif(regexp_replace(coalesce(v.geometrielabel,''),'[^[:alnum:] ]', '', 'g'), ''),
       nullif(regexp_replace(coalesce(v.bg_naam,''),      '[^[:alnum:] ]', '', 'g'), '')
    ), ''), 'zzzzgeenmatchzzzz'),
    ' ', 'gi'),
  '[^[:alnum:] ]', ' ', 'g'))
"""


def _handelingen_van(naam: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ÿ]+", naam.lower()) if w in _HANDELINGEN}


def _bouw_hulptabellen(conn) -> None:
    conn.execute("""
        CREATE TEMP TABLE wz_frase AS
        SELECT urn, lower(p) AS phrase,
               array_length(string_to_array(trim(lower(p)), ' '), 1) AS nwoorden,
               length(p) AS nchar
        FROM (SELECT urn, naam AS p FROM i2a.werkzaamheid
              UNION ALL SELECT urn, unnest(trefwoorden) FROM i2a.werkzaamheid) s
        WHERE p IS NOT NULL AND length(trim(p)) >= 4""")
    conn.execute("CREATE INDEX ON wz_frase (phrase)")
    conn.execute("""
        CREATE TEMP TABLE frase_ambigu AS
        SELECT phrase, count(DISTINCT urn) AS urns FROM wz_frase GROUP BY 1
        HAVING count(DISTINCT urn) <= %s""", (MAX_URNS_PER_FRASE,))
    conn.execute("CREATE INDEX ON frase_ambigu (phrase)")


def _bouw_doel(conn, where: str, params: tuple) -> int:
    conn.execute(f"""
        CREATE TEMP TABLE doel AS
        SELECT v.koop_id, v.activiteit_code, bg.overheidscode AS bg_overheidscode,
               {_TEKST_SQL} AS tekst
        FROM vth.vergunningkennisgeving v
        JOIN vth.bg_resolutie bg ON bg.bg_naam = v.bg_naam
                                AND bg.organisatietype = v.organisatietype
        WHERE bg.overheidscode IS NOT NULL AND ({where})""", params)
    conn.execute("CREATE INDEX ON doel (koop_id)")
    return conn.execute("SELECT count(*) AS c FROM doel").fetchone()["c"]


def _bouw_kandidaten(conn, n_doc: int) -> None:
    conn.execute("""
        CREATE TEMP TABLE ngram AS
        WITH doc AS (SELECT koop_id,
                            string_to_array(trim(regexp_replace(tekst,'\\s+',' ','g')),' ') arr
                     FROM doel)
        SELECT DISTINCT koop_id, x AS ng FROM (
          SELECT koop_id, arr[i] x FROM doc, generate_subscripts(arr,1) i
          UNION ALL SELECT koop_id, arr[i]||' '||arr[i+1] FROM doc, generate_subscripts(arr,1) i
                    WHERE i+1 <= cardinality(arr)
          UNION ALL SELECT koop_id, arr[i]||' '||arr[i+1]||' '||arr[i+2] FROM doc, generate_subscripts(arr,1) i
                    WHERE i+2 <= cardinality(arr)
        ) s WHERE length(x) >= 4""")
    conn.execute("CREATE INDEX ON ngram (ng)")

    drempel = max(1, int(n_doc * MAX_DF_FRACTIE))
    conn.execute("""
        CREATE TEMP TABLE frase_ok AS
        SELECT p.phrase FROM wz_frase p
        JOIN frase_ambigu a ON a.phrase = p.phrase
        JOIN ngram g ON g.ng = p.phrase
        GROUP BY 1 HAVING count(DISTINCT g.koop_id) <= %s""", (drempel,))
    conn.execute("CREATE INDEX ON frase_ok (phrase)")

    conn.execute("""
        CREATE TEMP TABLE kandidaat AS
        SELECT g.koop_id, p.urn,
               max(p.nwoorden * 100 + least(p.nchar, 99))::real AS basis_score,
               (array_agg(p.phrase ORDER BY p.nwoorden DESC, p.nchar DESC))[1] AS bewijs
        FROM ngram g
        JOIN wz_frase p ON p.phrase = g.ng
        JOIN frase_ok f ON f.phrase = p.phrase
        JOIN doel d ON d.koop_id = g.koop_id
        JOIN i2a.werkzaamheid_activiteit wa
          ON wa.werkzaamheid_urn = p.urn AND wa.overheid_ns = d.bg_overheidscode
        GROUP BY 1, 2""")
    conn.execute("CREATE INDEX ON kandidaat (koop_id)")


def _weeg(conn) -> dict[str, list[dict]]:
    """Pas rubriek-straf en handelings-bonus toe; geef per koop_id de rangorde."""
    namen = {r["urn"]: r["naam"] for r in
             conn.execute("SELECT urn, naam FROM i2a.werkzaamheid")}
    handelingen = {urn: _handelingen_van(n) for urn, n in namen.items()}

    # Groepen van de activiteiten waar een (werkzaamheid, overheid) op uitkomt.
    groepen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("""
            SELECT DISTINCT wa.werkzaamheid_urn, wa.overheid_ns, a.groep
            FROM i2a.werkzaamheid_activiteit wa
            JOIN p2p.activiteit a ON a.identificatie = wa.activiteit_urn
            WHERE wa.overheid_ns IN (SELECT DISTINCT bg_overheidscode FROM doel)
              AND a.groep IS NOT NULL"""):
        groepen[(r["werkzaamheid_urn"], r["overheid_ns"])].add(r["groep"])

    per_doc: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("""
            SELECT k.koop_id, k.urn, k.basis_score, k.bewijs,
                   d.activiteit_code, d.bg_overheidscode, d.tekst
            FROM kandidaat k JOIN doel d USING (koop_id)"""):
        score = r["basis_score"]

        toegestaan = _RUBRIEK_GROEPEN.get((r["activiteit_code"] or "").lower())
        if toegestaan:
            gevonden = groepen.get((r["urn"], r["bg_overheidscode"]), set())
            if gevonden and not (gevonden & toegestaan):
                score -= RUBRIEK_STRAF

        nodig = handelingen.get(r["urn"], set())
        if nodig and (nodig & set(r["tekst"].split())):
            score += HANDELING_BONUS

        per_doc[r["koop_id"]].append(
            {"urn": r["urn"], "score": score, "bewijs": r["bewijs"]})

    for koop_id, rijen in per_doc.items():
        rijen.sort(key=lambda x: (-x["score"], x["urn"]))
        for i, rij in enumerate(rijen, 1):
            volgende = rijen[i]["score"] if i < len(rijen) else None
            rij["rang"] = i
            rij["marge"] = None if volgende is None else rij["score"] - volgende
        per_doc[koop_id] = rijen[:MAX_RANG]
    return per_doc


def koppel(where: str = "type_besluit = 'verleend'", params: tuple = ()) -> dict:
    """Bereken en schrijf de koppelingen voor de geselecteerde vergunningen."""
    with get_conn() as conn:
        conn.row_factory = dict_row
        conn.execute("SET statement_timeout = '1800s'")
        _bouw_hulptabellen(conn)
        n_doc = _bouw_doel(conn, where, params)
        log.info("doelverzameling: %d vergunningen", n_doc)
        _bouw_kandidaten(conn, n_doc)
        per_doc = _weeg(conn)

        # Idempotent, en alléén de eigen methode: handmatige waarheid blijft.
        conn.execute("""
            DELETE FROM vth.vergunning_werkzaamheid
            WHERE methode = %s AND koop_id IN (SELECT koop_id FROM doel)""",
            (METHODE,))
        rijen = [
            (koop_id, r["urn"], r["rang"], r["score"], r["marge"], r["bewijs"],
             "tekst", METHODE, METHODE_VERSIE)
            for koop_id, lijst in per_doc.items() for r in lijst
        ]
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO vth.vergunning_werkzaamheid
                  (koop_id, werkzaamheid_urn, rang, score, marge, bewijs,
                   bewijs_veld, methode, methode_versie)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (koop_id, werkzaamheid_urn, methode) DO UPDATE SET
                  rang=EXCLUDED.rang, score=EXCLUDED.score, marge=EXCLUDED.marge,
                  bewijs=EXCLUDED.bewijs, methode_versie=EXCLUDED.methode_versie,
                  bepaald_op=now()""", rijen)
        conn.commit()
        return {"vergunningen": n_doc, "met_kandidaat": len(per_doc),
                "rijen": len(rijen)}
