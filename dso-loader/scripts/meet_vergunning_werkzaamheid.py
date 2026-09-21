"""Meet de keten omgevingsvergunning -> werkzaamheid -> activiteit -> regel -> tekst.

Vraag (2026-09-20/21): een KOOP-vergunningkennisgeving staat nu los van de
regels waaronder ze is verleend. Kunnen we die relatie leggen langs de
reguliere annotatieketen?

    kennisgeving (titel + beschrijving, B1-taal)
      -> Werkzaamheid            i2a.werkzaamheid (naam + trefwoorden)
      -> Activiteit              i2a.werkzaamheid_activiteit, gesneden op overheid
      -> ActiviteitLocatieaanduiding  p2p.activiteit_locatieaanduiding
      -> JuridischeRegel         p2p.juridische_regel
      -> Regeltekst              p2p.tekst_element via (wid, regeling_expression)
      -> Artikel of Lid

De eerste sprong is de enige die geraden moet worden; alles daarna is door
bronhouders gecureerd. Dit script meet per stap hoeveel er overblijft, en
hoe groot de artikelenlijst is die je uiteindelijk toont.

Gebruik:
    python scripts/meet_vergunning_werkzaamheid.py [--sample 1000]
                                                   [--tekstbron titel|volledig]
                                                   [--type-besluit verleend]

Draait in ~20 seconden op de standaard steekproef van 1.000.

Alle tussentabellen zijn TEMP: het script laat niets achter in de DB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg.rows import dict_row  # noqa: E402

from src.db import get_conn  # noqa: E402

# Een frase telt alleen mee als ze bij hoogstens zoveel werkzaamheden hoort.
# Zonder deze grens rust 94,7% van de beste treffers op een kaal werkwoord
# ("verwijderen" hangt aan 12 werkzaamheden) en is de keuze een muntworp.
MAX_URNS_PER_FRASE = 2


def bouw(conn, sample: int, type_besluit: str, tekstbron: str) -> None:
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
        CREATE TEMP TABLE frase_disc AS
        SELECT phrase, count(DISTINCT urn) AS urns FROM wz_frase GROUP BY 1""")
    conn.execute("CREATE INDEX ON frase_disc (phrase)")

    conn.execute("""
        CREATE TEMP TABLE kg AS
        SELECT v.koop_id, v.bg_naam, v.organisatietype, b.overheidscode AS overheid_ns,
               lower(regexp_replace(
                 CASE WHEN %s = 'volledig'
                      THEN coalesce(v.titel,'') || ' ' || coalesce(v.beschrijving,'')
                           || ' ' || left(coalesce(v.inhoud_tekst,''), 4000)
                      ELSE coalesce(v.titel,'') || ' ' || coalesce(v.beschrijving,'')
                 END, '[^[:alnum:] ]', ' ', 'g')) AS tekst
        FROM vth.vergunningkennisgeving v
        LEFT JOIN core.bronhouder b
          ON lower(b.naam) = lower(v.bg_naam) AND b.bestuurslaag = v.organisatietype
        WHERE v.type_besluit = %s
        ORDER BY random() LIMIT %s""", (tekstbron, type_besluit, sample))
    conn.execute("CREATE INDEX ON kg (koop_id)")

    conn.execute("""
        CREATE TEMP TABLE kg_ngram AS
        WITH w AS (SELECT koop_id, string_to_array(trim(regexp_replace(tekst,'\\s+',' ','g')),' ') arr FROM kg)
        SELECT DISTINCT koop_id, ng FROM (
          SELECT koop_id, arr[i] ng FROM w, generate_subscripts(arr,1) i
          UNION ALL SELECT koop_id, arr[i]||' '||arr[i+1] FROM w, generate_subscripts(arr,1) i
                    WHERE i+1 <= cardinality(arr)
          UNION ALL SELECT koop_id, arr[i]||' '||arr[i+1]||' '||arr[i+2] FROM w, generate_subscripts(arr,1) i
                    WHERE i+2 <= cardinality(arr)
        ) s WHERE length(ng) >= 4""")
    conn.execute("CREATE INDEX ON kg_ngram (ng)")

    # Kandidaat-werkzaamheden: discriminerende frase EN gekoppeld bij dit BG.
    conn.execute("""
        CREATE TEMP TABLE kandidaat AS
        SELECT g.koop_id, p.urn, max(p.nwoorden*100 + least(p.nchar,99)) AS score
        FROM kg_ngram g
        JOIN wz_frase p   ON p.phrase = g.ng
        JOIN frase_disc d ON d.phrase = p.phrase AND d.urns <= %s
        JOIN kg k         ON k.koop_id = g.koop_id
        JOIN i2a.werkzaamheid_activiteit wa
          ON wa.werkzaamheid_urn = p.urn AND wa.overheid_ns = k.overheid_ns
        GROUP BY 1,2""", (MAX_URNS_PER_FRASE,))
    conn.execute("CREATE INDEX ON kandidaat (koop_id)")

    conn.execute("""
        CREATE TEMP TABLE gekozen AS
        WITH s AS (SELECT koop_id, urn, score, max(score) OVER (PARTITION BY koop_id) mx FROM kandidaat),
             t AS (SELECT koop_id, count(*) FILTER (WHERE score = mx) aan_top FROM s GROUP BY 1)
        SELECT s.koop_id, s.urn FROM s JOIN t USING (koop_id)
        WHERE t.aan_top = 1 AND s.score = s.mx""")
    conn.execute("CREATE INDEX ON gekozen (koop_id)")

    conn.execute("""
        CREATE TEMP TABLE gekozen_tolerant AS
        WITH s AS (SELECT koop_id, urn, score, max(score) OVER (PARTITION BY koop_id) mx FROM kandidaat)
        SELECT koop_id, urn FROM s WHERE score = mx""")
    conn.execute("CREATE INDEX ON gekozen_tolerant (koop_id)")

    conn.execute("""
        CREATE TEMP TABLE act_tolerant AS
        SELECT DISTINCT g.koop_id, wa.activiteit_urn
        FROM gekozen_tolerant g JOIN kg k USING (koop_id)
        JOIN i2a.werkzaamheid_activiteit wa
          ON wa.werkzaamheid_urn = g.urn AND wa.overheid_ns = k.overheid_ns""")
    conn.execute("CREATE INDEX ON act_tolerant (koop_id)")

    # Variant C — een echte top-N: de N best scorende werkzaamheden, ook als
    # ze lager scoren dan de winnaar. Dat is wat "toon een top 5" betekent.
    conn.execute("""
        CREATE TEMP TABLE gerangschikt AS
        SELECT koop_id, urn, score,
               row_number() OVER (PARTITION BY koop_id ORDER BY score DESC, urn) AS rang
        FROM kandidaat""")
    conn.execute("CREATE INDEX ON gerangschikt (koop_id, rang)")

    conn.execute("""
        CREATE TEMP TABLE act_rang AS
        SELECT DISTINCT g.koop_id, g.rang, wa.activiteit_urn
        FROM gerangschikt g JOIN kg k USING (koop_id)
        JOIN i2a.werkzaamheid_activiteit wa
          ON wa.werkzaamheid_urn = g.urn AND wa.overheid_ns = k.overheid_ns""")
    conn.execute("CREATE INDEX ON act_rang (koop_id, rang)")

    conn.execute("""
        CREATE TEMP TABLE act AS
        SELECT DISTINCT g.koop_id, wa.activiteit_urn
        FROM gekozen g JOIN kg k USING (koop_id)
        JOIN i2a.werkzaamheid_activiteit wa
          ON wa.werkzaamheid_urn = g.urn AND wa.overheid_ns = k.overheid_ns""")
    conn.execute("CREATE INDEX ON act (activiteit_urn)")
    conn.execute("CREATE INDEX ON act (koop_id)")

    # De laatste twee sprongen. tekst_element.wid is NIET uniek over regelingen
    # (zie gaps G-144): joinen op wid alleen levert artikelen uit vreemde
    # regelingen op. Daarom altijd mee op regeling_expression.
    conn.execute("""
        CREATE TEMP TABLE regel AS
        SELECT DISTINCT a.koop_id, ala.juridische_regel_id, ala.kwalificatie
        FROM act a
        JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = a.activiteit_urn""")
    conn.execute("CREATE INDEX ON regel (juridische_regel_id)")
    conn.execute("CREATE INDEX ON regel (koop_id)")

    conn.execute("""
        CREATE TEMP TABLE tekst AS
        SELECT DISTINCT r.koop_id, r.kwalificatie, te.id AS tekst_element_id,
               te.element_type, te.eid, te.regeling_expression
        FROM regel r
        JOIN p2p.juridische_regel jr ON jr.identificatie = r.juridische_regel_id
        JOIN p2p.tekst_element te
          ON te.wid = jr.regeltekst_wid
         AND te.regeling_expression = jr.regeling_expression""")
    conn.execute("CREATE INDEX ON tekst (koop_id)")


def rapporteer(conn, sample: int) -> None:
    def scalar(sql: str) -> int:
        return list(conn.execute(sql).fetchone().values())[0]

    stappen = [
        ("kennisgevingen in de steekproef", f"SELECT {sample}"),
        ("bevoegd gezag geresolved", "SELECT count(*) FROM kg WHERE overheid_ns IS NOT NULL"),
        ("minstens een werkzaamheid-kandidaat", "SELECT count(DISTINCT koop_id) FROM kandidaat"),
        ("een eenduidige werkzaamheid", "SELECT count(*) FROM gekozen"),
        ("bereikt een activiteit", "SELECT count(DISTINCT koop_id) FROM act"),
        ("bereikt een juridische regel", "SELECT count(DISTINCT koop_id) FROM regel"),
        ("bereikt regeltekst (artikel of lid)", "SELECT count(DISTINCT koop_id) FROM tekst"),
    ]
    tolerant = [
        ("gelijkspel toegestaan: records", "SELECT count(DISTINCT koop_id) FROM gekozen_tolerant"),
        ("  daarvan toch 1 activiteit",
         "SELECT count(*) FROM (SELECT koop_id FROM act_tolerant GROUP BY 1 "
         "HAVING count(DISTINCT activiteit_urn) = 1) x"),
        ("  daarvan <= 3 activiteiten",
         "SELECT count(*) FROM (SELECT koop_id FROM act_tolerant GROUP BY 1 "
         "HAVING count(DISTINCT activiteit_urn) <= 3) x"),
    ]
    print(f"{'stap':<40} {'records':>9} {'% steekproef':>13}")
    print("-" * 64)
    for naam, sql in stappen:
        n = scalar(sql)
        print(f"{naam:<40} {n:>9,} {100.0*n/sample:>12.1f}%".replace(",", "."))

    print()
    for naam, sql in tolerant:
        n = scalar(sql)
        print(f"{naam:<40} {n:>9,} {100.0*n/sample:>12.1f}%".replace(",", "."))

    print("\nvariant B — alleen gelijkspelers, cumulatief naar aantal activiteiten")
    for n in (1, 2, 3, 5, 10):
        k = scalar("SELECT count(*) FROM (SELECT koop_id FROM act_tolerant "
                   f"GROUP BY 1 HAVING count(DISTINCT activiteit_urn) <= {n}) x")
        print(f"  <= {n:>2} activiteiten {k:>9,} {100.0*k/sample:>11.1f}%".replace(",", "."))

    print("\nvariant C — echte top-N werkzaamheden op score")
    print(f"  {'N':>3} {'records':>9} {'%':>7} {'med.act':>8} {'med.art':>8} {'p90.art':>8}")
    for n in (1, 2, 3, 5, 10):
        r = conn.execute(f"""
            WITH sel AS (SELECT koop_id, activiteit_urn FROM act_rang WHERE rang <= {n}),
                 per AS (SELECT koop_id, count(DISTINCT activiteit_urn) n_act FROM sel GROUP BY 1),
                 art AS (SELECT s.koop_id, count(DISTINCT te.id) n_art
                         FROM sel s
                         JOIN p2p.activiteit_locatieaanduiding ala
                              ON ala.activiteit_id = s.activiteit_urn
                         JOIN p2p.juridische_regel jr
                              ON jr.identificatie = ala.juridische_regel_id
                         JOIN p2p.tekst_element te
                              ON te.wid = jr.regeltekst_wid
                             AND te.regeling_expression = jr.regeling_expression
                             AND te.element_type = 'Artikel'
                         GROUP BY 1)
            SELECT (SELECT count(*) FROM art) AS records,
                   (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY n_act) FROM per) AS med_act,
                   (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY n_art) FROM art) AS med_art,
                   (SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY n_art) FROM art) AS p90_art
            """).fetchone()
        print(f"  {n:>3} {r['records']:>9,} {100.0*r['records']/sample:>6.1f}% "
              f"{r['med_act']:>8} {r['med_art']:>8} {r['p90_art']:>8}".replace(",", "."))

    print("\nomvang van de artikelenlijst per vergunning")
    rows = conn.execute("""
        SELECT count(*) AS records,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY n_art) AS mediaan_artikelen,
               percentile_disc(0.9) WITHIN GROUP (ORDER BY n_art) AS p90_artikelen,
               max(n_art) AS max_artikelen,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY n_el) AS mediaan_elementen
        FROM (SELECT koop_id,
                     count(DISTINCT tekst_element_id) FILTER (WHERE element_type='Artikel') AS n_art,
                     count(DISTINCT tekst_element_id) AS n_el
              FROM tekst GROUP BY 1) x""").fetchone()
    for k, v in rows.items():
        print(f"  {k:<22} {v}")

    print("\nop welk element hangt de annotatie")
    for r in conn.execute("""SELECT element_type, count(*) n FROM tekst
                             GROUP BY 1 ORDER BY n DESC LIMIT 6"""):
        print(f"  {r['element_type']:<16} {r['n']:>9,}".replace(",", "."))

    print("\nkwalificatie van de bereikte regels")
    for r in conn.execute("""SELECT lower(kwalificatie) AS kwalificatie, count(DISTINCT koop_id) n
                             FROM tekst GROUP BY 1 ORDER BY n DESC LIMIT 8"""):
        print(f"  {str(r['kwalificatie']):<32} {r['n']:>8,}".replace(",", "."))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    # 1.000 is ruim genoeg: de 95%-betrouwbaarheidsmarge op een aandeel rond
    # 60% is dan ~3 procentpunt, en we kiezen hier een knop tussen 58% en
    # 85%. Grotere steekproeven kosten kwartieren en scherpen niets aan.
    ap.add_argument("--sample", type=int, default=1000)
    ap.add_argument("--type-besluit", default="verleend")
    ap.add_argument("--tekstbron", choices=("titel", "volledig"), default="titel",
                    help="titel = titel+beschrijving; volledig = plus inhoud_tekst")
    args = ap.parse_args()

    with get_conn() as conn:
        conn.row_factory = dict_row
        conn.execute("SET statement_timeout = '900s'")
        bouw(conn, args.sample, args.type_besluit, args.tekstbron)
        rapporteer(conn, args.sample)


if __name__ == "__main__":
    main()
