# Uniform zoektermen-contract — API-implementatie-kant

**Datum**: 2026-05-10
**Status**: ontwerp, nog niet geïmplementeerd
**Companion**: zie ook `C:\GIT\omgevingsbot.nl\docs\20260510_uniform-zoektermen-contract.md`
voor de orchestratie-kant.

## Waarom dit doc bestaat

De omgevingsbot-pipeline geeft op zijn tekst-endpoints (`/v1/regeltekst`,
`/v1/onderwerp`) een volledige `zoektermen: list[str]` mee, maar op de
object-endpoints (`/v1/normwaarde`, `/v1/activiteit`, `/v1/bestemming`)
ofwel één detector-string ofwel niets. Dit doc beschrijft hoe OCD-API de
object-endpoints uitbreidt zodat ze hetzelfde keyword-contract krijgen,
**zonder backward-compat te breken**.

## Huidige API-contract (samenvatting)

| Endpoint | Filter-input vandaag | SQL-filter |
|---|---|---|
| `/v1/regeltekst` | `q: str` (FTS-query) | `to_tsvector('dutch', tekst_element.inhoud) @@ to_tsquery(...)` |
| `/v1/onderwerp` | `q: str` (woorden) | `gebiedsaanwijzing.naam ILIKE ANY(...) OR type ILIKE ANY(...) OR groep ILIKE ANY(...)` |
| `/v1/normwaarde` | `naam: str` (één term) | `p2p.norm.naam ILIKE '%<naam>%'` |
| `/v1/activiteit` | `soort: str` (één term) | `p2p.activiteit.naam ILIKE '%<soort>%'` |
| `/v1/bestemming` | — | alleen `ST_Intersects` op (x,y) |

## Voorgestelde uitbreiding

### Per endpoint

#### `/v1/normwaarde`

```
GET /v1/normwaarde?
    x=<float>&y=<float>
    [&naam=<str>]                          # detector-pad (huidig)
    [&zoektermen=<csv>]                    # NIEUW: keyword-pad
    [&limit_detector=5&limit_keyword=15]   # NIEUW: aparte limits
```

SQL-filter:
```sql
WHERE ST_Intersects(loc.geometrie, ST_SetSRID(ST_MakePoint(:x, :y), 28992))
  AND (
        -- detector-bucket (preferred)
        (:naam IS NOT NULL AND p2p.norm.naam ILIKE '%' || :naam || '%')
     OR
        -- keyword-bucket (fallback)
        (:zoektermen IS NOT NULL AND (
              p2p.norm.naam  ILIKE ANY(:zoektermen_patterns)
           OR p2p.norm.groep ILIKE ANY(:zoektermen_patterns)
        ))
      )
ORDER BY
   (CASE WHEN p2p.norm.naam ILIKE '%' || :naam || '%' THEN 0 ELSE 1 END),  -- detector eerst
   p2p.norm.naam
```

Response: bestaande JSON-shape + per hit `"match_bucket": "detector" | "keyword"`,
+ in top-level `count_detector` en `count_keyword`.

`:zoektermen_patterns` = elk keyword omgeven door `%` (server-side te bouwen,
client geeft kale strings).

#### `/v1/activiteit`

Idem patroon, filter op `p2p.activiteit.naam` + `p2p.activiteit.groep`:

```sql
WHERE ST_Intersects(...)
  AND (
        (:soort IS NOT NULL AND p2p.activiteit.naam ILIKE '%' || :soort || '%')
     OR
        (:zoektermen IS NOT NULL AND (
              p2p.activiteit.naam  ILIKE ANY(:zoektermen_patterns)
           OR p2p.activiteit.groep ILIKE ANY(:zoektermen_patterns)
        ))
      )
```

#### `/v1/bestemming`

Nieuw veld toegevoegd (was zonder content-filter):

```
GET /v1/bestemming?x=<float>&y=<float>[&zoektermen=<csv>][&limit=20]
```

SQL — let op: Wro-schema, kolomnaam-aanname verifiëren:
```sql
WHERE ST_Intersects(planobject.geometrie, ...)
  AND (
        :zoektermen IS NULL
     OR planobject.bestemming_hoofd  ILIKE ANY(:zoektermen_patterns)
     OR planobject.bestemming_dubbel ILIKE ANY(:zoektermen_patterns)
     -- of welke kolomnamen ook in de wro-schema staan
      )
```

#### `/v1/coverage`

Bestaande `onderwerp: str` blijft. Optioneel een `zoektermen`-parameter
toevoegen die als OR-uitbreiding meedoet — laagste prioriteit, kan ook
in fase 2.

### Response-shape verandering

Alle drie endpoints retourneren naast de huidige velden:

```json
{
  "count": 6,
  "count_detector": 1,
  "count_keyword": 5,
  "matches": [
    {
      "identificatie": "...",
      "naam": "maximum bouwhoogte",
      "match_bucket": "detector",
      ...
    },
    {
      "identificatie": "...",
      "naam": "maximum goothoogte",
      "match_bucket": "keyword",
      ...
    }
  ]
}
```

Bestaande clients die `count` en `matches` lezen blijven werken — extra velden
zijn additief.

## Backward-compat

Alle nieuwe parameters zijn **optioneel met default None/leeg**. Bestaand
gedrag:

- Geen `zoektermen` meegegeven → endpoint gedraagt zich exact als vandaag.
- `naam` of `soort` zonder `zoektermen` → exact als vandaag.
- `zoektermen` zonder `naam`/`soort` → nieuw keyword-only gedrag.
- Beide → uniform-bucket gedrag.

Geen breaking change op response-shape: alleen extra velden.

## Index-strategie

De huidige object-tabellen hebben (waarschijnlijk) geen FTS- of trigram-index
op `naam`/`groep`. ILIKE-met-wildcard zonder index = sequential scan, schaalt
slecht op grote `p2p.activiteit`-tabellen.

**Aanbeveling**: trigram-index op alle filter-velden via `pg_trgm`. Jullie
hebben de migratie-infra al (`2026-05-add-trgm-index.sql`).

```sql
-- voorbeeld
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_p2p_norm_naam_trgm
    ON p2p.norm USING gist (naam gist_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_p2p_norm_groep_trgm
    ON p2p.norm USING gist (groep gist_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_p2p_activiteit_naam_trgm
    ON p2p.activiteit USING gist (naam gist_trgm_ops);
-- etc.
```

**Niet upfront alles maken** — eerst meten. Bij <100k rijen per tabel kan
een sequential scan acceptabel zijn. Index pas toevoegen bij merkbare
latentie-stijging in `inspect_ocd_pipeline`-runs.

## Implementatie-volgorde

1. **Parameter-parsing in FastAPI route-handlers** (klein, ~30 LoC per endpoint).
   `zoektermen` accepteren als CSV-string of als repeated query-param
   (`?zoektermen=hoog&zoektermen=max`).
2. **SQL-builder uitbreiden** in `main.py` of waar `_wat_geldt_hier` /
   gerelateerde query-builders staan. Twee aparte WHERE-clauses (detector,
   keyword) gecombineerd met OR.
3. **Result-mapping**: `match_bucket` per rij bepalen + tellers in response.
4. **OpenAPI-schema** bijwerken zodat clients (omgevingsbot) typed clients
   kunnen genereren.
5. **Trigram-indexen optioneel** — alleen waar nodig na latentie-meting.

## Test-aanpak

In `ocd-api/tests/` per endpoint testen:

- Geen filters → alle objects op (x,y).
- Alleen `naam`/`soort` → exact als huidig gedrag.
- Alleen `zoektermen=["X","Y"]` → matches op `naam` of `groep` met die termen.
- Beide → detector-hits krijgen `match_bucket="detector"`, keyword-hits
  `match_bucket="keyword"`; `count_detector` en `count_keyword` kloppen.
- Edge: lege `zoektermen=[]` → behandel als None.
- Edge: korte termen in `zoektermen=["een","de"]` — verwacht: filter zelf
  bouwt geen patterns voor termen <4 chars, of laat dat aan de client.

Integratie-haak: `omgevingsbot-backend` heeft een `inspect_ocd_pipeline`
endpoint dat de bucket-counts mee terug rapporteert; daar valt L1-eval op te
draaien om regressie op recall te meten.

## Risico's en open vragen

1. **Wro-schema kolomnamen.** `/v1/bestemming` raakt het Wro/IMRO-schema.
   Welke velden zijn relevant voor filtering? `planobject.bestemming`?
   `planobject.functie_aanduiding`? Even verifiëren in `wro_loader.py`.
2. **Performance op grote bronhouders.** Een Omgevingsplan met 5k+
   activiteiten + 1k normen kan op ILIKE-ANY zonder index merkbaar trager
   worden. Meten met query-EXPLAIN voor 1-2 grote gemeenten voordat indexen
   structureel landen.
3. **CSV vs repeated param.** Beide zijn FastAPI-natief; voorkeur is
   `?zoektermen=a&zoektermen=b` omdat het URL-encoding van komma's en
   spaties simpeler maakt.
4. **Wat te doen met zoektermen die identiek aan `naam`/`soort` zijn?**
   Same string in beide buckets: hit valt in detector-bucket (preferred);
   keyword-bucket telt 'm niet apart. SQL-volgorde regelt dat al via
   `CASE WHEN detector THEN 0 ELSE 1`.

## Geschat werk

~½ tot 1 dag inclusief tests en OpenAPI-update, exclusief eventuele
trigram-indexen + performance-tuning.

## Wat dit NIET oplost

- **Echte SKOS-URI-filtering op objects.** Zoals besproken: `p2p.activiteit`,
  `p2p.gebiedsaanwijzing`, `p2p.norm` hebben geen `thema_uri`-veld. Alleen
  `p2p.tekstdeel.thema` is SKOS-verbonden via `core.imow_thema`. Dit uniform-
  contract werkt op string-match (synoniemen + SKOS-concept-namen die naar
  strings worden gevouwen), niet op URI-niveau.
- **Loader-aanpassingen.** Niets in de loaders verandert. Alleen de query-laag
  in `ocd-api`.
- **Hiërarchische SKOS-expansie** (broader/narrower). Dit niveau van semantiek
  hoort in de keyword-extract-call zelf (`/v1/keywords/extract`), niet in de
  filter-endpoints.
