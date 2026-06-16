"""Genereer `ocd-api/action_words.py` — de set Nederlandse actie-werkwoord-vormen.

OFFLINE bouwstap (NIET in de runtime-hot-path): vereist spaCy + het NL-model en
toegang tot de `skos`-schema in de OCD-database. Draai 'm opnieuw als de
SKOS-vocabulaire substantieel wijzigt:

    pip install spacy && python -m spacy download nl_core_news_sm
    python tools/build_action_words.py

Methode (zie analyse 2026-06-16):
  - Tag ALLEEN de Werkzaamheden-conceptNAMEN in context met spaCy. Daar leeft
    het "<object> <actie>"-patroon ("Brug aanpassen", "Bouwwerk onderhouden");
    andere schema's zitten vol gemeentenamen/znw's die spaCy als VERB mistagt.
  - Neem alleen de woordVORM van VERB-tokens (geen lemma -> vermijdt spaCy-
    lemma-typo's als "motoriseeren").
  - Trek znw's af die ergens als NOUN/PROPN in de namen voorkomen (znw wint =
    veilige faalrichting: liever een werkwoord missen dan een inhouds-znw demoten).
  - `-en`-infinitief-poort: weg met fragmenten (afwij), vervoegde vormen (maakt),
    deelwoorden/bijv.nw (-end/-de) en namen die niet op -en eindigen (Bilt).
  - EXCLUDE: kleine met-de-hand-nagekeken lijst meervoud-znw's die als object in
    een actie-naam staan en toch als VERB werden gemistagd (zonnepanelen, ...).
  - SEED: generieke actie-synoniemen die als losse vraag-term opduiken maar niet
    in de Werkzaamheden-namen staan (bv. "veranderen").

De runtime gebruikt deze set puur als lookup; spaCy is GEEN runtime-dependency.
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row

URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/dso")

# Generieke actie-synoniemen die als losse vraag-term opduiken maar niet als
# verb in de Werkzaamheden-namen staan. Bewust klein en onomstreden generiek.
SEED = {"veranderen", "wijzigen", "aanpassen", "slopen", "vernieuwen",
        "verwijderen", "renoveren", "herstellen", "repareren", "opknappen",
        "plaatsen", "ombouwen", "uitbreiden", "verplaatsen", "verleggen"}

# Meervoud-znw's die als object in een "<object> <actie>"-naam staan en door
# spaCy als VERB werden gemistagd. Met de hand nagekeken — geen acties.
EXCLUDE = {"biociden", "gootsteen", "langshaven", "maden", "oplosmiddelen",
           "sportvelen", "waterdieren", "zonnepanelen"}


def main() -> None:
    try:
        import spacy
    except ImportError:
        sys.exit("spaCy ontbreekt — `pip install spacy && python -m spacy download nl_core_news_sm`")
    nlp = spacy.load("nl_core_news_sm")

    with psycopg.connect(URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT LOWER(naam) AS naam FROM skos.concept "
            "WHERE scheme_naam = 'Werkzaamheden' AND naam IS NOT NULL"
        )
        namen = [r["naam"] for r in cur.fetchall()]

    action_words: set[str] = set()
    noun_words: set[str] = set()
    for doc in nlp.pipe(namen, batch_size=256):
        for t in doc:
            if t.pos_ == "VERB":
                action_words.add(t.text)          # alleen woordvorm, geen lemma
            elif t.pos_ in ("NOUN", "PROPN"):
                noun_words.add(t.text)
    action_words -= noun_words
    action_words = {w for w in action_words if w.endswith("en") and len(w) > 3}
    action_words -= EXCLUDE
    action_words |= SEED

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "action_words.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('"""Gegenereerd door tools/build_action_words.py — NIET handmatig bewerken.\n\n')
        f.write("Set Nederlandse actie-werkwoord-vormen, afgeleid uit de Werkzaamheden-\n")
        f.write("conceptnamen. Gebruikt door keywords.py om werkwoord-matches te\n")
        f.write('onderscheiden van inhoud(znw)-matches bij het selecteren van begrippen.\n"""\n\n')
        f.write("ACTION_WORDS: frozenset[str] = frozenset({\n")
        for w in sorted(action_words):
            f.write(f"    {w!r},\n")
        f.write("})\n")

    print(f"{len(namen)} Werkzaamheden-namen -> {len(action_words)} ACTION_WORDS -> {out_path}")


if __name__ == "__main__":
    main()
