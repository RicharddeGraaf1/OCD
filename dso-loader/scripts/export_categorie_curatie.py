"""Exporteert de gecureerde categorieën naar JSON, zodat een herbouw ze terugkrijgt.

`build_categorie.py` draait een DDL die `v2a.categorie` DROPt. De tabel is dus
niet de bewaarplaats van menselijk oordeel, hoe stellig het DDL-commentaar ook
"first-class, compounding" zegt. Dit script schrijft dat oordeel weg naar
`curatie/categorie-curatie.json`; `build_categorie.py` leest het na het seeden
van de IMOW-ruggengraat weer in.

Wat er wél in gaat: de door mensen gepromoveerde categorieën (`bron='gebruiker'`)
met hun centroïde, naam en thema. De centroïde is het enige stabiele anker —
`categorie_id` van een discovery-kandidaat is `kandidaat.<clusternummer>` en dat
nummer verschilt per HDBSCAN-run, dus daar kun je niets op vastzetten.

Wat er NIET in gaat: afgekeurde kandidaten. Een volgende run stelt andere
clusters voor; een afkeuring van vandaag zegt niets over een cluster van morgen.
Die beoordeel je opnieuw — en dat is goedkoop, want alleen `bevestigd` telt mee
in de toewijzing.

Draaien: python scripts/export_categorie_curatie.py
"""

import io
import json
import os

import psycopg

DB = "postgresql://postgres:postgres@localhost:5434/dso"
UIT = os.path.join(os.path.dirname(__file__), "..", "curatie", "categorie-curatie.json")


def main() -> None:
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.categorie_id, k.naam, p.naam AS thema, k.n_chunks_seen,
                   k.centroide::text, k.taxonomie_versie, k.naam_auto
            FROM v2a.categorie k
            LEFT JOIN v2a.categorie p ON p.categorie_id = k.parent_id
            WHERE k.bron = 'gebruiker' AND k.status = 'bevestigd'
            ORDER BY p.naam, k.naam
            """
        )
        rijen = [
            {
                "naam": naam,
                "thema": thema,
                "n_chunks_seen": n or 0,
                # thema-naam, niet parent_id: de id van een IMOW-thema is afgeleid
                # van het label en dus stabiel, maar de naam leest beter in een
                # bestand dat een mens moet kunnen nakijken.
                "centroide": [float(x) for x in cent.strip("[]").split(",")],
                "herkomst": {"taxonomie_versie": versie, "auto_naam": auto},
            }
            for cid, naam, thema, n, cent, versie, auto in cur.fetchall()
        ]

    os.makedirs(os.path.dirname(UIT), exist_ok=True)
    io.open(UIT, "w", encoding="utf-8", newline="\n").write(
        json.dumps({"categorieen": rijen}, ensure_ascii=False, indent=1)
    )
    print(f"{len(rijen)} gecureerde categorieën -> {os.path.normpath(UIT)}")


if __name__ == "__main__":
    main()
