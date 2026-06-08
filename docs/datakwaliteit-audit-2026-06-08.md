# Datakwaliteit-audit OCD-database — 2026-06-08

> Read-only audit + fixes naar aanleiding van data-/loaderfouten die
> retrieval-metingen (semantische-index-PoC voor de Omgevingsbot)
> *obfusceerden*. Leidend principe: **grip + niet-obfusceerde metingen** —
> elke fix moet het onderscheid scherper maken tussen "de aanpak werkt niet"
> en "de data klopt niet". Niet perfecte data.

## Hoofdconclusie

Na de eerder deze sessie gefixte parser-bug en bronhouder-naamcorrecties is de
data **structureel gezond**. De obfuscatie kwam grotendeels van fouten die nu
al weg zijn. Drie resterende, concreet adresseerbare zaken zijn aangepakt:
26 ontbrekende regelingen herladen, een **load-status** en een **data-health-
laag** toegevoegd, en een leeg `locatiegroep_lid` gediagnosticeerd.

| # | Categorie | Oordeel | Bevinding |
|---|---|---|---|
| 1 | Bronhouder-integriteit | ✅ schoon | 0 naam-misalignments; duplicaat-twins + stubs verdwenen (527→511) |
| 2 | Load-volledigheid | 🟡→✅ | 0 lege actieve regelingen; 26 ontbrekend vs DSO → **herladen** |
| 3 | Geo-/punt-dekking | ✅ geen blinde vlekken | 11,4% dode scopes, **0% echte blinde vlek** (steekproef 300) |
| 4 | Annotatiedichtheid | 🟡 hol = content-realiteit | 92,8% brede scope · 89,8% "anders geduid" · 44% artikel-dekking |

## Audit 1 — Bronhouder-integriteit · SCHOON

- 12 provincies ✓ (CBS + opschrift), gemeentenamen ✓ **100% vs PDOK**
  (`core.gemeentegrens`), waterschap/rijk ✓ vs regeling-opschrift.
- 0 code-only labels, 0 NULL-`bestuurslaag`.
- De 14 duplicaat-twins (lege ws-codes naast de content-dragende) en 14
  pseudo-stubs (`gm0000`/`gm99xx`) zijn tijdens de auditsessie door een
  parallel proces opgeschoond (bronhouder-telling 527 → 511).

**Root cause (structureel geborgd):** `naam` kwam uit een handmatig CLI-argument
+ `ON CONFLICT DO NOTHING` → een typefout bleef eeuwig plakken (pv21 stond ooit
op "Groningen" i.p.v. "Fryslân"). **Fix A** borgt provincienamen centraal en
gebruikt `ON CONFLICT … DO UPDATE` voor gecontroleerde lagen — zie
`src/loaders/ow_loader.py` (`PROVINCIE_NAMEN`, `canonieke_bronhouder_naam`).

## Audit 2 — Load-volledigheid · GOED, 1 gat gedicht

- **0 actieve regelingen zonder tekst** (alle 15 documenttypes, 1886 actieve
  regelingen) — de parser-bug liet geen half-geladen regelingen achter.
- **DSO-vs-p2p coverage** (live Presenteren-API, `diff_dso_bronhouder_coverage.py`):
  - pv/rijk/waterschap (39 bronhouders): **1 ontbrekend** (*Omgevingsvisie
    provincie Groningen*, pv20); 6 "overbodig" bij mnre1153 = stale/ingetrokken
    N2000-besluiten.
  - gemeenten (342): **25 ontbrekend** over 21 gemeenten (gm0373 +4, gm0784 +2,
    rest +1). Vrijwel allemaal recente (2025/2026) Programma's,
    Voorbeschermingsregels en 2 Omgevingsvisies → normale load-lag.
  - **Totaal 26 regelingen herladen** via `scripts/laad_specifieke_regelingen.py`.
- **Fix C — load-status per regeling**: nieuwe tabel `p2p.regeling_load`
  (status `ok`/`partieel`/`gefaald`, tellingen, timestamp, laatste_fout).
  Backfilled uit huidige tellingen; de loader schrijft er voortaan een rij per
  regeling aan het eind van `_load_from_zip`. Maakt stille load-fouten zichtbaar.

## Audit 3 — Geo-/punt-dekking · GEEN ECHTE BLINDE VLEKKEN

- Geo-index schoon: 6,44M subdiv-stukjes, **0 orphans**, **0 NULL-geometrie**,
  31.562 vindbare locaties.
- 11,4% (3.590) "dode geo-scopes" zonder annotatie — gedomineerd door
  Gebiedengroep (2477) en Ambtsgebied (601), beide containers/aggregaten.
- **Beslissende meting**: steekproef van 300 dode scopes met echte geometrie →
  **100% ligt óók onder een geannoteerde locatie**. **0% echte blinde vlek.**
  → *Een lage retrieval-score is nooit toe te schrijven aan geo-dekking.*

## Audit 4 — Annotatiedichtheid · HOL (= content-realiteit, nu meetbaar)

| Metric | Waarde |
|---|---|
| Locatie-discriminatie (`activiteit_locatieaanduiding`) | 72,4% Ambtsgebied + 20,4% Gebiedengroep = **92,8% brede scope**; 7,2% specifiek Gebied |
| Kwalificatie-specificiteit | **89,8% "anders geduid"**; ~10% betekenisvol |
| Artikel-dekking (juridische_regel) | **44%** (gem 43 / prov 52 / ws 54 / rijk 42) |

Grotendeels bruidsschat-realiteit, geen bug — nu als **getal** vastgelegd zodat
een lage score herkenbaar is als content-realiteit i.p.v. een bug.

## Fix D — `locatiegroep_lid` is leeg (gediagnosticeerd, lage prio)

`p2p.locatiegroep_lid` bevat **0 rijen** voor alle 9415 Gebiedengroepen.
Root cause: de primaire gemeente-laadroute `api_loader.load_via_api`
(`cli.py` load-via-api) schrijft Gebiedengroep-*locaties* wél maar de
groep→lid-relaties **niet** — alleen de oudere ZIP-route
`ow_loader._load_from_zip` parseert leden (`l:groepselement/l:GebiedRef`).
Impact bevestigd **minimaal** (Audit 3: groep-annotaties + lid-geometrieën
overlappen, 0% blinde vlekken). **Aanbevolen follow-up**: in `api_loader` de
lid-referenties uit de Presenteren-respons lezen en `locatiegroep_lid` vullen,
óf na een bulk-load aanvullen uit de ZIP-route.

## Data-health-laag (de kern van het verzoek)

Migratie: `dso-loader/scripts/2026-06-add-data-health.sql` (idempotent).

| Object | Doel |
|---|---|
| `p2p.regeling_load` | load-status per regeling (Fix C) |
| `core.bronhouder_dso_diff` | DSO-coverage-gat per bronhouder; gevoed door `diff_dso_bronhouder_coverage.py --persist` |
| `core.mv_bronhouder_health` | **1 rij per bronhouder**: load, naam-integriteit, annotatiedichtheid |
| `core.v_data_health` | top-samenvatting met drempel-flags |
| `core.v_geo_health` | globale geo-dekking (dode scopes, orphans, locatiegroep_lid) |

**API**: `GET /v1/data-health` (ocd-api):
- geen params → `v_data_health` + `v_geo_health` samenvatting
- `?bronhouder=pv25` → die ene rij
- `?problemen=true` → alleen bronhouders met een integriteits-/load-flag

**Staande rapportage / wekelijkse cyclus:**
1. `python scripts/diff_dso_bronhouder_coverage.py --prefix pv,mn,ws,gm --persist`
   (schrijft `core.bronhouder_dso_diff`).
2. `REFRESH MATERIALIZED VIEW core.mv_bronhouder_health;`
3. Controleer `GET /v1/data-health?problemen=true` — leeg = schoon.

Zo zie je dataproblemen **vóór** ze een meting vervuilen, niet erna.

## Onderhoudssnippets

```sql
-- Gemeente-namen herijken op PDOK (idempotent; nu 0 diffs):
UPDATE core.bronhouder b SET naam = g.naam
FROM core.gemeentegrens g
WHERE g.overheidscode = b.overheidscode AND b.naam <> g.naam;

-- Health verversen:
REFRESH MATERIALIZED VIEW core.mv_bronhouder_health;
```
