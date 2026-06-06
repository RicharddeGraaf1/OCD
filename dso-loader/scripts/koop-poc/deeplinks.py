"""
Wrapper-module — deeplink-detectie en -validatie voor KOOP-vergunningen.

De canonical implementatie leeft sinds 2026-05-31 in
`src/loaders/koop_deeplinks.py`. Deze wrapper re-exporteert alle publieke
namen zodat de bestaande PoC-scripts in deze map (`ingest.py`,
`backfill_deeplinks.py`, `validate_deeplinks.py`) en eventuele
shell-aliassen ongewijzigd blijven werken.

Niet hier verder ontwikkelen — alle wijzigingen horen in
`src/loaders/koop_deeplinks.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Zorg dat `src.loaders.koop_deeplinks` importeerbaar is wanneer scripts
# direct vanuit deze map worden gestart (zonder package-install).
_ROOT = Path(__file__).parent
_DSO_LOADER_ROOT = _ROOT.parent.parent  # c:/GIT/OCD/dso-loader/
if str(_DSO_LOADER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DSO_LOADER_ROOT))

from src.loaders.koop_deeplinks import *  # noqa: F401,F403,E402
from src.loaders.koop_deeplinks import (  # noqa: E402  re-export voor IDE's
    DEEPLINK_HOSTS,
    extract_deeplinks,
    host_of,
    make_client,
    upsert_deeplink,
    validate_url,
)

__all__ = [
    "DEEPLINK_HOSTS",
    "extract_deeplinks",
    "host_of",
    "make_client",
    "upsert_deeplink",
    "validate_url",
]
