"""Retry-helper voor transiënte fouten op de DSO-API's.

De DSO geeft met enige regelmaat een losse `503 Service Unavailable` terug.
Zonder retry breekt zo'n hik een hele fase af: op 2026-08-01 sneuvelde de
BOPA-snapshot op pagina 62 van 78 (~3.000 records niet verwerkt) en viel één
gemeente uit de i2a-fase — beide door één 503.

Bewust **beperkt**: een paar pogingen met oplopende wachttijd, nooit een strakke
lus. Alleen transiënte fouten worden herprobeerd (5xx, timeouts, verbroken
verbindingen); een 4xx is een echte fout en gaat direct door.

De rate-limiter zit in de aanroepende functie, niet hier — een retry gaat dus
netjes opnieuw door de limiter heen.
"""

import time
from typing import Callable, TypeVar

import httpx
from rich.console import Console

console = Console()

T = TypeVar("T")

# Oplopend, en de laatste ruim genoeg om een korte storing te overleven.
BACKOFF_SEQ = (2, 5, 15, 30)


def is_transient(exc: BaseException) -> bool:
    """Alleen fouten waarbij opnieuw proberen zin heeft."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def met_retry(fn: Callable[[], T], omschrijving: str = "DSO-call",
              backoff: tuple[int, ...] = BACKOFF_SEQ) -> T:
    """Voer `fn` uit; herprobeer bij transiënte fouten volgens `backoff`.

    Gooit de laatste exception door als alle pogingen op zijn — een fase hoort
    zichtbaar te falen als de DSO echt plat ligt, niet stil door te gaan.
    """
    pogingen = len(backoff) + 1
    for i in range(pogingen):
        try:
            return fn()
        except Exception as e:
            if not is_transient(e) or i == pogingen - 1:
                raise
            wacht = backoff[i]
            code = (e.response.status_code
                    if isinstance(e, httpx.HTTPStatusError) else type(e).__name__)
            console.print(f"    [yellow]{omschrijving}: {code} — poging "
                          f"{i + 1}/{pogingen}, opnieuw over {wacht}s[/yellow]")
            time.sleep(wacht)
    raise AssertionError("onbereikbaar")  # pragma: no cover
