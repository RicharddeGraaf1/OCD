"""Tests voor de peildatum van de RTR/STTR-calls (`imtr_loader._peildatum`).

Aanleiding: de `datum`-parameter stond van de initial commit tot 2026-08-09
hardgecodeerd op `10-04-2026`. RTR en STTR zijn geldigheidsgestuurd, dus i2a
laadde vier maanden lang de april-toestand — gemeten over 19 bronhouders stond
daardoor ~1,7% van de inhoud stil en kwamen nieuwe regelbestanden niet binnen.

Deze tests bewaken twee dingen: dat de standaard meebeweegt met de dag, en dat
het formaat blijft wat de API verwacht (`dd-mm-yyyy`, niet ISO).
"""

import re
from datetime import date

from src.loaders import imtr_loader


def test_standaard_is_vandaag(monkeypatch):
    monkeypatch.setattr(imtr_loader.cfg, "IMTR_PEILDATUM", "", raising=False)
    assert imtr_loader._peildatum() == date.today().strftime("%d-%m-%Y")


def test_formaat_is_dag_maand_jaar(monkeypatch):
    """Niet ISO: de STTR geeft op `2026-08-09` een lege lijst, geen fout."""
    monkeypatch.setattr(imtr_loader.cfg, "IMTR_PEILDATUM", "", raising=False)
    assert re.fullmatch(r"\d{2}-\d{2}-\d{4}", imtr_loader._peildatum())


def test_env_override_wint(monkeypatch):
    """Ontsnapping om een oude toestand te reproduceren."""
    monkeypatch.setattr(imtr_loader.cfg, "IMTR_PEILDATUM", "10-04-2026",
                        raising=False)
    assert imtr_loader._peildatum() == "10-04-2026"


def test_geen_hardgecodeerde_datum_meer():
    """Regressie: de drie call-sites mogen geen vaste datum meer bevatten."""
    from pathlib import Path
    bron = Path(imtr_loader.__file__).read_text(encoding="utf-8")
    # De docstrings noemen 10-04-2026 als geschiedenis; de code mag hem niet
    # meer als parameterwaarde gebruiken.
    assert '"datum": "10-04-2026"' not in bron
    assert bron.count('"datum": _peildatum()') == 3
