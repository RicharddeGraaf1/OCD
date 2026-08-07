"""Stopwoord-filter in de keyword-extractie.

Geen database nodig: `_tokenize` is puur lexicaal. `extract_vraag_chips` heeft
wel een cursor nodig voor de SKOS-frequentie; die wordt hier gemockt zodat de
test de *selectie* toetst en niet de inhoud van de SKOS-graaf.
"""
from __future__ import annotations

import keywords as mod


class FakeCursor:
    """Doet alsof geen enkele term in SKOS voorkomt (freq 0).

    Dat is precies het scenario waarin het misging: de 1-gram-drempel weert
    termen die in véél concepten zitten, dus freq 0 passeert altijd. Alleen de
    stopwoordenlijst kan een vraagwoord dan nog tegenhouden.
    """

    def execute(self, *_args, **_kwargs) -> None:
        pass

    def fetchall(self) -> list[dict]:
        return []


class TestVraagwoorden:
    def test_welke_valt_weg_uit_de_tokens(self):
        assert mod._tokenize("welke regels gelden hier over datacentra") == ["datacentra"]

    def test_geen_chip_voor_welke_of_de_combinatie(self):
        chips = mod.extract_vraag_chips(FakeCursor(), "welke regels gelden hier over datacentra")

        assert "datacentra" in chips
        assert not any("welke" in c for c in chips), chips

    def test_andere_vraagwoorden_ook(self):
        for vraag, verwacht in [
            ("wanneer mag ik een dakkapel plaatsen", "dakkapel"),
            ("hoeveel woningen mogen hier", "woningen"),
            ("waarom geldt hier een aanlegvergunning", "aanlegvergunning"),
            ("waarin verschilt dit van een schuur", "schuur"),
        ]:
            tokens = mod._tokenize(vraag)
            assert verwacht in tokens, (vraag, tokens)
            assert not (set(tokens) & mod.STOP_WORDS), (vraag, tokens)

    def test_inhoudswoorden_blijven(self):
        # De uitbreiding mag geen vaktermen raken.
        for woord in ["datacentra", "dakkapel", "geluidgevoelig", "windturbine",
                      "aanlegsteiger", "hoogspanningsverbinding"]:
            assert woord not in mod.STOP_WORDS

    def test_alles_stopwoord_geeft_lege_tokenlijst(self):
        # Vraag zonder enige inhoud — de aanroeper hoort hierop terug te vallen.
        assert mod._tokenize("welke gelden hier eigenlijk") == []
