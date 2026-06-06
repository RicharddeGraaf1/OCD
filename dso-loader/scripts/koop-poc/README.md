# KOOP omgevingsvergunning-kennisgevingen — PoC loader

PoC voor de ingest van omgevingsvergunning-kennisgevingen uit
**Officielebekendmakingen.nl (KOOP)** naar het **losstaande `vth`-schema
in OCD** (Postgres), met optionele SQLite-backend voor lokaal debuggen.

Achtergrond, bron-realiteit, schema-ontwerp en modelimpact staan in
`vault_v1/analysis/Ingest omgevingsvergunningen uit officielebekendmakingen.md`
en `vault_v1/model.md §14`. Het structurele gat dat deze loader
onvermijdelijk maakt (geen landelijk register van verleende vergunningen
onder Ow) staat in `vault_v1/gaps.md G-70`.

## Waarom hier en niet in `src/loaders/`?

De doel-class `Vergunningkennisgeving` zit in `model.md §14` nog als
**voorstel/te-verifiëren** (de pilaar-positie zelf is niet definitief —
mogelijk gaat hij later op in een bredere Uitvoering-pilaar). Zolang
dat zo is, leeft de loader hier als losstaand script i.p.v. als
geïntegreerde `src/loaders/`-loader met CLI-commando in `src/cli.py`.

Wel is het **schema al wel echt geland in Postgres**: `vth.*` is een
losstaand schema in dezelfde OCD-DB, met PostGIS-geometrie, **maar zonder
FK's** naar `dso.*` of andere pilaren (gebruiker-keuze 2026-05-19:
"alles mag volledig losstaand, niet uitvoerig koppelen in dit stadium").

Promotie naar `src/loaders/koop_vergunning.py` + CLI-commando volgt
zodra de pilaar van voorstel naar gevalideerd wordt gepromoveerd.

## Quick start

```bash
# Vanuit de dso-loader root, met de bestaande venv:
.venv\Scripts\activate

cd scripts/koop-poc

# Eerste keer: vth-schema aanmaken in Postgres
python ingest.py setup

# Eén dag ingesten (default backend: postgres)
python ingest.py run --from 2026-05-13 --to 2026-05-13

# Lokaal debuggen tegen SQLite (data/koop.db)
python ingest.py run --from 2026-05-13 --to 2026-05-13 --db sqlite

# Een week
python ingest.py run --from 2026-05-12 --to 2026-05-18

# Volledige backfill vanaf inwerkingtreding Ow (~820k records, ~2,5 uur)
python ingest.py run --from 2024-01-01 --to 2026-05-18

# Enrichment-pass: haal volledige publicatie-XML op (body-tekst + zaaknummer + adres-fallback)
python ingest.py enrich --limit 100         # 100 records met inhoud_geladen_at IS NULL
python ingest.py enrich --limit 5000        # tot 5000 in één pass

# Status / verdeling bekijken (postgres default)
python ingest.py status
python ingest.py status --db sqlite
```

Postgres-config komt uit de standaard dso-loader `.env` (DB_HOST, DB_PORT,
DB_NAME, DB_USER, DB_PASSWORD). Voor SQLite gaat data naar
`data/koop.db` (niet in git).

## Twee-fasen ingest

| Fase | Subcommand | Wat doet het | Snelheid |
|---|---|---|---|
| 1. Metadata | `run` | KOOP SRU bevragen (200/page), metadata + geometrie + activiteit-code + type_besluit + adres (waar gestructureerd) → DB | ~100 records/sec |
| 2. Inhoud | `enrich` | Voor records met `inhoud_geladen_at IS NULL`: download `xml_url`, parse body-tekst, extract zaaknummer, fallback adres uit titel | ~4 records/sec |

Fase 1 gaat vlot (volledige backfill 2024→nu in ~2,5 uur). Fase 2 is
één HTTP-request per record en duurt ~6 uur voor 820k records. Beide
zijn idempotent en restartable.

## Wat de loader doet

1. **Bron**: KOOP SRU 2.0 op `https://repository.overheid.nl/sru`.
2. **Filter**: `dt.type="omgevingsvergunning"` (rubriek
   `OVERHEIDop.Rubriek`) + dag-gefilterd op `dt.modified`.
3. **Paginering**: 200 records per request, restartable per dag via
   `vth.etl_run`-tabel.
4. **Per record geparst**: KOOP-id, bevoegd gezag (naam + scheme),
   publicatieblad (gmb/prb/wsb/stcrt), datum, titel, rubriek,
   activiteit-code (uit waardelijst `OVERHEIDop.ActiviteitOmgevingsvergunning`),
   type besluit (classifier op titel), gestructureerde locatie.
5. **Locatie-parsing per type**:
   - `Adres` -> POINT (RD + WGS84) + huisnummer + postcode + straatnaam
     + woonplaats.
   - `Punt` -> POINT (RD + WGS84).
   - `Vlak` -> POLYGON (RD + WGS84) **plus** centroid POINT voor
     map-pinning, adres uit `geometrielabel`-string.
   - Geen gebiedsmarkering -> alleen tekstvelden.
6. **Idempotent** op `koop_id`:
   - Postgres: `INSERT … ON CONFLICT (koop_id) DO UPDATE`
   - SQLite: `INSERT OR REPLACE`
7. **PostGIS-geometrie** via `ST_GeomFromText(%s, SRID)` (Postgres).
   In SQLite-modus worden geometrieën als WKT-strings opgeslagen.
8. **Volledig raw XML** bewaard voor latere her-parsing.

## Performance

- ~10 sec per dag (~2000 records gemiddeld).
- Volledige backfill 2024-01-01 → vandaag (~820k records) doet ~2,5 uur.

## type_besluit-classifier

Regex-based op de titel. Volgorde matters (specifiek voor generic).
Categorieën: `rectificatie`, `van_rechtswege`, `verlenging_beslistermijn`,
`ingetrokken`, `geweigerd`, `ontwerp`, `verleend`, `melding_geaccepteerd`,
`melding`, `aanvraag`, `kennisgeving`, `overig`.

Geen classifier is 100% — handhaving en bijzondere gevallen kunnen op
`overig` landen. Verbeter door extra patterns toe te voegen in
`TYPE_BESLUIT_RULES`.

## Bekende beperkingen

- Vlak-centroid is een rekenkundig gemiddelde van vertices, geen echte
  zwaartepunt. Voor PoC voldoende; bij Postgres-migratie kan
  `ST_Centroid(ST_GeomFromText(geometrie_rd_wkt, 28992))` exact zijn.
- `geometrielabel`-adresparser is best-effort. Werkt voor de meeste
  "Straat 12, 1234AB Plaats"-vormen; gaat onderuit op bijzondere
  notaties (kadastraal kenmerk, meerdere adressen, beschrijvende
  locaties).
- Geen rate-limiting nodig in praktijk (KOOP accepteerde 200/page
  zonder issues), maar `REQUEST_INTERVAL=0.25s` als courtesy.
- Geen retry-logica voor partial-dag-failures binnen één dag — als
  paginering halverwege faalt, krijgt de hele dag een 'error' status
  en kan met `--force` opnieuw worden gestart.
