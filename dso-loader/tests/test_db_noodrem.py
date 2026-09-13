"""Tests voor de noodrem op een weggevallen database tijdens de delta-sweep.

Achtergrond: op 2026-09-12 om 23:52:32 werkte de Microsoft Store WSL bij naar
2.7.14.0. `wsl.msi` legde `vmmem.exe` om, en daarmee de Postgres-container. De
sync was acht seconden eerder aan zijn delta-sweep begonnen; de preflight
(eenmalig, vóór de sweep) was nog groen.

`load_delta` ving elke exception per bronhouder op en ging door. Gevolg: 119
bronhouders op rij met `connection timeout expired`, elk keurig gelogd, de fase
die gewoon doorliep, en niemand die het merkte tot iemand naar het log keek.

Een fout bij één bronhouder mag de andere 380 niet blokkeren — dat blijft zo.
Een dode database is een andere soort fout, en die breekt de sweep nu af.
"""

import psycopg
import pytest

from src.loaders import api_loader
from src.loaders.api_loader import DatabaseOnbereikbaar


# ── _is_db_verbindingsfout ───────────────────────────────────────────

def test_herkent_operational_error():
    assert api_loader._is_db_verbindingsfout(psycopg.OperationalError("weg"))


def test_herkent_ingepakte_operational_error():
    """De loader verpakt fouten soms; dan zit de echte oorzaak dieper."""
    try:
        try:
            raise psycopg.OperationalError("connection timeout expired")
        except psycopg.OperationalError as oorzaak:
            raise RuntimeError("laden mislukt") from oorzaak
    except RuntimeError as e:
        assert api_loader._is_db_verbindingsfout(e)


def test_dso_rommel_is_geen_verbindingsfout():
    assert not api_loader._is_db_verbindingsfout(ValueError("rare respons"))


def test_geen_oneindige_lus_bij_cyclische_oorzaak():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert not api_loader._is_db_verbindingsfout(a)


# ── de sweep zelf ────────────────────────────────────────────────────

def _sweep(monkeypatch, fouten_per_bronhouder, n_bronhouders=20):
    """Draai load_delta met een gemockte loader en lijst-call.

    `fouten_per_bronhouder` is een functie index -> exception of None.
    """
    regelingen = [{"bronhouder_code": f"gm{i:04d}", "bronhouder_naam": f"BH{i}"}
                  for i in range(n_bronhouders)]
    monkeypatch.setattr(api_loader, "find_regelingen_delta",
                        lambda *a, **kw: regelingen)

    aangeroepen = []

    def nep_load(overheid_code, naam, **kw):
        i = len(aangeroepen)
        aangeroepen.append(overheid_code)
        fout = fouten_per_bronhouder(i)
        if fout:
            raise fout

    monkeypatch.setattr(api_loader, "load_via_api", nep_load)
    return aangeroepen


def test_breekt_af_na_vijf_db_fouten_op_rij(monkeypatch):
    aangeroepen = _sweep(monkeypatch,
                         lambda i: psycopg.OperationalError("connection timeout expired"))
    with pytest.raises(DatabaseOnbereikbaar):
        api_loader.load_delta(None)
    assert len(aangeroepen) == api_loader.MAX_OPEENVOLGENDE_DB_FOUTEN, \
        "de sweep hoort te stoppen op de vijfde, niet door te malen"


def test_losse_dso_fouten_stoppen_de_sweep_niet(monkeypatch):
    """Twintig bronhouders die allemaal rommel leveren: sweep loopt door."""
    aangeroepen = _sweep(monkeypatch, lambda i: ValueError("rare respons"))
    res = api_loader.load_delta(None)
    assert len(aangeroepen) == 20
    assert all(v.startswith("error:") for v in res.values())


def test_geslaagde_bronhouder_zet_de_teller_terug(monkeypatch):
    """Vier fouten, dan een succes, dan weer vier: dat is geen dode database."""
    def fout(i):
        if i in (4,):
            return None
        return psycopg.OperationalError("weg")

    aangeroepen = _sweep(monkeypatch, fout, n_bronhouders=9)
    res = api_loader.load_delta(None)
    assert len(aangeroepen) == 9
    assert res["gm0004"] == "ok"
