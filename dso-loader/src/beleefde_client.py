"""Beleefde HTTP-client voor bulkwerk tegen de DSO-API's.

Dit staat bewust náást `src/rate_limiter.py` + `src/http_retry.py` en vervangt
die niet. Die twee zijn afgestemd op de gewone sync: duizenden calls, waarbij
een losse 503 een hik is die je moet uitzitten. Voor een crawl van tienduizenden
calls is dat gedrag juist verkeerd:

* **De DSO geeft bij overbelasting 503 en géén 429 met Retry-After**
  (`docs/dso-llms.txt`). Een client kan "je gaat te snel" dus niet onderscheiden
  van "de dienst is stuk". Bij die twijfel hoor je te wijken, niet te herhalen.
* `met_retry()` herprobeert elke 5xx vier keer (2/5/15/30 s). Op 52.000
  bestanden kan dat de belasting vervijfvoudigen precies op het moment dat de
  dienst het moeilijk heeft.

Daarom hier het omgekeerde beleid:

* één verbinding, hergebruikt (`max_connections=1`) — dat is tegelijk
  *minder* serverwerk (geen TLS-handshake per call) en een harde garantie dat
  er nooit twee requests tegelijk lopen;
* een laag vast tempo met jitter, ver onder het gedocumenteerde budget;
* bij een 503 lang pauzeren, en bij herhaling de run afbreken — niet doorduwen.

Gedocumenteerd budget van het stelsel (`docs/dso-llms.txt`): 50 gelijktijdige
verbindingen, 50 requests/s, voorkeursvenster 22:00–06:00 CET. Deze client
gebruikt standaard 1 req/s: 2% daarvan.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from rich.console import Console

from src.config import cfg

console = Console()

# Het stelsel kent zijn budget toe aan clients die zich melden met
# `<naam>/<versie> (+<project-url>; <contactadres>)`. Zonder contactadres zijn
# we een anonieme bot en is blokkeren voor een beheerder de enige optie.
# Het adres is bewust NIET hard opgenomen: zet DSO_CONTACT in .env.
_NAAM = "ocd-loader"
_VERSIE = "0.1"
_PROJECT_URL = os.getenv("DSO_PROJECT_URL", "https://ocd-viewer.nl")
_CONTACT = os.getenv("DSO_CONTACT", "").strip()


def user_agent() -> str:
    if _CONTACT:
        return f"{_NAAM}/{_VERSIE} (+{_PROJECT_URL}; {_CONTACT})"
    return f"{_NAAM}/{_VERSIE} (+{_PROJECT_URL})"


class DienstWijktAf(RuntimeError):
    """De dienst gaf herhaald 503 — de run stopt uit zichzelf."""


@dataclass
class Beleefd:
    """Serieel, traag, en stopt liever dan dat hij doordrukt.

    tempo            requests per seconde (1.0 = 2% van het budget)
    max_503          aantal 503'en binnen `venster_503` voordat we afbreken
    pauze_503        seconden pauzeren na een 503 voordat we het nog één keer
                     proberen
    alleen_s_nachts  weiger te draaien buiten 22:00-06:00 lokale tijd
    """

    tempo: float = 1.0
    max_503: int = 2
    pauze_503: float = 120.0
    venster_503: float = 900.0
    timeout: float = 60.0
    alleen_s_nachts: bool = False

    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _laatste: float = field(default=0.0, init=False, repr=False)
    _503s: list[float] = field(default_factory=list, init=False, repr=False)
    calls: int = field(default=0, init=False)

    # -- levenscyclus ---------------------------------------------------
    def __enter__(self) -> "Beleefd":
        if self.alleen_s_nachts and not in_voorkeursvenster():
            raise DienstWijktAf(
                "Buiten het voorkeursvenster 22:00-06:00 CET. Start met "
                "alleen_s_nachts=False als je dit bewust overdag wilt doen."
            )
        if not _CONTACT:
            console.print(
                "[yellow]Let op: DSO_CONTACT staat niet in .env, dus we melden ons "
                "zonder contactadres. Het stelsel vraagt er expliciet om.[/yellow]"
            )
        self._client = httpx.Client(
            timeout=self.timeout,
            headers={"x-api-key": cfg.DSO_API_KEY, "User-Agent": user_agent()},
            # Precies één verbinding: hergebruikt (minder serverwerk) en
            # tegelijk een harde garantie tegen parallellisme.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        return self

    def __exit__(self, *exc) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- tempo ----------------------------------------------------------
    def _wacht(self) -> None:
        interval = 1.0 / self.tempo
        # Jitter, zodat het geen strak machinaal patroon vormt.
        doel = self._laatste + interval * random.uniform(0.85, 1.15)
        nu = time.monotonic()
        if doel > nu:
            time.sleep(doel - nu)
        self._laatste = time.monotonic()

    def _noteer_503(self) -> None:
        nu = time.monotonic()
        self._503s = [t for t in self._503s if nu - t < self.venster_503]
        self._503s.append(nu)
        if len(self._503s) >= self.max_503:
            raise DienstWijktAf(
                f"{len(self._503s)}x 503 binnen {self.venster_503 / 60:.0f} min. "
                "De DSO geeft bij overbelasting 503 en geen 429, dus we kunnen "
                "'te snel' niet van 'kapot' onderscheiden. Run afgebroken; "
                "later hervatten (het werk is gecheckpoint)."
            )

    # -- de enige call --------------------------------------------------
    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        """Eén GET. Bij 503: één keer lang pauzeren, daarna opgeven."""
        assert self._client is not None, "gebruik `with Beleefd() as c:`"
        for poging in (1, 2):
            self._wacht()
            self.calls += 1
            try:
                r = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if poging == 2:
                    raise
                console.print(f"    [yellow]{type(e).__name__} — één keer opnieuw "
                              f"over {self.pauze_503:.0f}s[/yellow]")
                time.sleep(self.pauze_503)
                continue

            if r.status_code == 503:
                self._noteer_503()  # gooit DienstWijktAf bij herhaling
                if poging == 2:
                    raise DienstWijktAf("503 bleef staan na een lange pauze.")
                console.print(f"    [yellow]503 — {self.pauze_503:.0f}s pauzeren "
                              f"voor één laatste poging[/yellow]")
                time.sleep(self.pauze_503)
                continue

            if r.status_code == 429:
                # Zou volgens de documentatie niet voorkomen. Als het tóch
                # gebeurt is het een ondubbelzinnig signaal: meteen stoppen.
                raise DienstWijktAf(
                    "429 ontvangen — ondubbelzinnig te snel. Run afgebroken."
                )
            return r
        raise AssertionError("onbereikbaar")  # pragma: no cover


def in_voorkeursvenster(nu: datetime | None = None) -> bool:
    """22:00-06:00 lokale tijd, het venster dat het stelsel zelf noemt."""
    u = (nu or datetime.now()).hour
    return u >= 22 or u < 6
