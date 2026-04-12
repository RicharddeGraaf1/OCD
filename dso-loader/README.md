# DSO Loader

Load all DSO (Digitaal Stelsel Omgevingswet) content into a Postgres+PostGIS database.

**PoC**: Utrecht (CBS 0344) — Ow-regelingen, toepasbare regels (IMTR), en Wro-bestemmingsplannen.

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+ with PostGIS 3+
- A DSO API key (request at https://developer.omgevingswet.overheid.nl/formulieren/api-key-aanvragen-0/)

### Setup

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows

# Install dependencies
pip install -e .

# Copy and edit .env
copy .env.example .env
# Edit .env with your API key and DB credentials

# Create database
createdb dso
psql -d dso -c "CREATE EXTENSION postgis;"

# Setup schema + lookup tables
python -m src.cli setup
```

### Load data

```bash
# Load Wro bestemmingsplannen from PDOK (no API key needed)
python -m src.cli load-wro

# Load toepasbare regels (needs API key)
python -m src.cli load-imtr

# Load Ow regelingen (needs API key) — coming soon
python -m src.cli load-ow

# Check what's loaded
python -m src.cli status
```

## Data Sources

| Source | API | Auth | What you get |
|---|---|---|---|
| **PDOK** | ATOM feed GML.GZ downloads | None | Wro bestemmingsplannen (IMRO GML) |
| **DSO RTR** | REST (HAL+JSON) | API key | Activiteiten + regelBeheerObjecten |
| **DSO STTR** | REST (HAL+JSON + XML) | API key | Toepasbare regelbestanden (DMN XML) |
| **DSO Ozon** | REST (ZIP) | API key | Ow-regelingen (STOP XML + OW objects) |

## Database Schema

~40 tables covering 5 pillars: STOP (text), CIM-OW (objects), IMTR (decision trees), LVBB (metadata), Wro/IMRO (old regime).

Run `python -m src.cli status` to see row counts.

## Architecture

See `vault_v1/analysis/Datamodel v1.0 DDL.md` and `vault_v1/analysis/Datamodel v1.0 ERD.md` for the full schema documentation.
