"""Configuration from .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")


class Config:
    # DSO API
    DSO_API_KEY: str = os.getenv("DSO_API_KEY", "")

    # DSO base URLs
    DSO_DOWNLOAD_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/downloaden/v1"
    PRESENTEREN_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8"
    GEOMETRIE_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/geometrieopvragen/v1"
    CATALOGUS_BASE = "https://service.omgevingswet.overheid.nl/publiek/catalogus/api/opvragen/v3"
    RTR_BASE = "https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/rtrgegevens/v2"
    STTR_BASE = "https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/toepasbareregelsuitvoerengegevens/v1"

    # IHR (Informatiehuis Ruimte) for Wro planteksten
    IHR_BASE = "https://ruimte.omgevingswet.overheid.nl/ruimtelijke-plannen/api/opvragen/v4"
    IHR_API_KEY: str = os.getenv("IHR_API_KEY", "")

    # PDOK
    PDOK_ATOM_BASE = "https://service.pdok.nl/kadaster/ruimtelijke-plannen/atom/downloads"

    # Database
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "dso")
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "postgres")

    @property
    def db_url(self) -> str:
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    # PoC municipality
    POC_CBS_CODE: str = os.getenv("POC_CBS_CODE", "0344")
    POC_OIN: str = os.getenv("POC_OIN", "00000001002220647000")
    POC_GEMEENTE_NAAM: str = os.getenv("POC_GEMEENTE_NAAM", "Utrecht")

    # Paths
    DOWNLOAD_DIR: Path = _project_root / os.getenv("DOWNLOAD_DIR", "data/downloads")
    CACHE_DIR: Path = _project_root / os.getenv("CACHE_DIR", "data/cache")

    # Rate limiting (legacy per-loader sleeps, kept for ow_loader ZIP pipeline)
    DSO_RATE_LIMIT: float = 0.15
    # Shared rate limiter: see src/rate_limiter.py (50 concurrent, 50/s)


cfg = Config()
