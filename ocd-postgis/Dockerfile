# PostGIS-image voor de OCD-productiedatabase op Coolify (Hetzner).
#
# Vastgezet op een stabiele PostGIS-versie. Railway draaide postgis/postgis:17-master:
# een tag die meeschuift, met PostGIS 3.7.0dev. Daarnaast pgvector voor
# v2a.tekst_embedding; die zat niet in het image van Railway.
# Zie docs/railway-naar-hetzner-plan.md.
FROM postgis/postgis:17-3.6

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
