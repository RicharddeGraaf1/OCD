"""Tests voor de tweede locatie-ingang van de prod-replicatie (vault G-141).

Achtergrond: bij de sync van 2026-09-18 gaf `repliceer_p2p_naar_prod.py` exit 0
terwijl prod 8 locaties miste — de pons-gebieden van Utrecht en Zeist en zes
programma-gebieden. Hun enige gemeenschappelijke eigenschap: geen juridische
regel, gebiedsaanwijzing of tekstdeel wijst ernaar, dus de FK-graaf van de
scope kwam er nooit langs. Ze stonden wél op de kaart.

De ingang vergelijkt per bronhouder `(identificatie, md5(geometrie))` aan
beide kanten. Wat hier getest wordt is de beslissing welke rijen mee moeten.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("PROD_DB_URL", "postgresql://niet-gebruikt")

_pad = Path(__file__).resolve().parent.parent / "scripts" / "repliceer_p2p_naar_prod.py"
_spec = importlib.util.spec_from_file_location("repliceer_p2p_naar_prod", _pad)
repl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repl)


def test_ontbrekende_locatie_gaat_mee():
    lok = {"nl.imow-gm0344.gebiedengroep.a": "h1", "nl.imow-gm0344.gebied.b": "h2"}
    prod = {"nl.imow-gm0344.gebied.b": "h2"}
    ontbreekt, anders = repl.te_spiegelen(lok, prod)
    assert ontbreekt == ["nl.imow-gm0344.gebiedengroep.a"]
    assert anders == []


def test_andere_geometrie_gaat_mee():
    """Zelfde id, andere geometrie: dat ziet een telling nooit (vault G-142)."""
    ontbreekt, anders = repl.te_spiegelen({"x": "nieuw"}, {"x": "oud"})
    assert ontbreekt == []
    assert anders == ["x"]


def test_gelijk_gaat_niet_mee():
    """Anders zou elke sync alle locaties van een provincie herschrijven."""
    assert repl.te_spiegelen({"x": "h", "y": "h"}, {"x": "h", "y": "h"}) == ([], [])


def test_alleen_op_prod_wordt_niet_aangeraakt():
    """Het script verwijdert niets; een prod-only rij is geen opdracht."""
    assert repl.te_spiegelen({}, {"x": "h"}) == ([], [])


def test_locatie_zonder_geometrie():
    """md5(ST_AsBinary(NULL)) is NULL. Aan beide kanten NULL is gelijk; van NULL
    naar een geometrie is een wijziging die mee moet."""
    assert repl.te_spiegelen({"x": None}, {"x": None}) == ([], [])
    assert repl.te_spiegelen({"x": "h"}, {"x": None}) == ([], ["x"])
