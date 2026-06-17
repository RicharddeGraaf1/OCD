# Retrieval-convergentie bot ↔ viewer (richting B)

Stand: **2026-06-17**. Branch: `feat/retrieval-kernel-convergentie` (OCD) +
`feat/intent-fastpath-viewer` (OCDviewer). Volledige analyse + meet-historie:
vault `analysis/Plan refactor gedeelde retrieval-laag bot en viewer`.

## Waarom

De omgevingsbot en de OCD-viewer beantwoordden dezelfde vraag ("wat geldt hier bij
deze vraag") via **twee verschillende retrieval-paden**, met aparte SKOS-afhandeling
en ranking → elke verbetering moest 2× of werd vergeten. Doel: **één gedeelde
retrieval-engine** (de bot-engine bleek de betere — 61,5% vs viewer-`killer_query`
57,7% op de retrieval-eval), zodat fixes één keer landen.

## Wat nu gedeeld is

| laag | gedeelde bron |
|---|---|
| Intent-detectie | `intent.py` (`detect_norm/activiteit/bestemming/intent`). `/v1/keywords/extract` geeft `norm_naam/soort/bestemming/intent` terug; de bot consumeert dat (lokale `_detect_*` = fallback). |
| Gewogen SKOS | `keywords.build_scored_keywords` (woordsoort × relevantie). Viewer native; bot via `_rank_by_relevance`/engine. Default-aan (`BOT_USE_WEIGHTED_SKOS`), +3,7pp antwoord-eval. |
| Ranking | `rank.py` `rank_regelteksten` (heuristiek + gewogen SKOS + bestuurslaag/source/overview/fts; MIN_RELEVANCE 2.0). **Géén BM25** (rank_bm25 nergens geïnstalleerd → bot paste 't toch nooit toe). |
| Retrieval-engine | `_wat_geldt_hier` (brede ophaal) + `rank_regelteksten`. |
| Norm-fast_path | `fastpaths.norm_fast_path` (deterministisch, conservatief) via `/v1/fast-path` + kernel. |

## Endpoints

- **`/v1/vraag-op-locatie`** (POST) — gedeelde kernel: brede ophaal + intent + fast_path
  + gewogen SKOS + rank. Levert `RegeltekstHit`-vorm (incl. `regeling_expression`, `wid`).
  **De viewer gebruikt dit** i.p.v. `killer_query` (achter frontend-flag `botEngineRetrieval`).
- **`/v1/adres` + `/v1/locatie`** met optionele **`vraag`-param** — "ranking meeleveren":
  rankt de net-opgehaalde regels server-side en voegt `ranked` (top-K, XML-gestript) toe.
  **De bot gebruikt dit** (zijn bestaande call) → geen tweede fetch, geen grote payload.
- `/v1/rank` (POST) — rank-only primitive (rank meegestuurde rijen). Niet runtime
  gebruikt door de bot (grote payload op hoog-volume locaties); blijft als hulpmiddel.
- `/v1/regelteksten-bij-vraag` + `/v1/antwoord-bij-vraag` — viewer-endpoints; `regelteksten-bij-vraag`
  draait door naar de gedeelde engine bij `botEngineRetrieval`.

## Flags & defaults

| flag | waar | default | effect |
|---|---|---|---|
| `BOT_USE_KERNEL` | bot env | **true** | bot haalt `ranked` uit `/v1/adres+vraag` i.p.v. lokale `_rank_by_relevance`. Veilige fallback: geen `ranked` → lokale rank. |
| `BOT_USE_WEIGHTED_SKOS` | bot env | **true** | gewogen SKOS in de bot-rank. |
| `botEngineRetrieval` | viewer env | dev **true** / prod **false** | viewer → `/v1/vraag-op-locatie`. Prod-uit tot OCD-prod gedeployed is. |
| `KERNEL_BROAD_ARM` | OCD env | false | brede regelingsgebied-arm in `/v1/regelteksten-bij-vraag` (union killer_query ⊕ tekst_fallback). |

## Bekende beperkingen / TODO

- **r24 + r27 regresseren** licht t.o.v. de bot's eigen rank: de gedeelde engine mist de
  bot-**augmentaties** (semantisch-prepend, `/v1/regeltekst`-FTS-boost, `/v1/onderwerp`-narrow)
  die `_rank_by_relevance` wél als input krijgt. Aggregaat blijft binnen LLM-ruis (±2-3pp),
  maar deze twee zijn echt. **TODO:** die augmentaties de engine in porten → herstelt r24/r27
  én tilt de viewer op (die had ze niet).
- **Cluster C** (provinciale visies/programma's, r34/r38/r39): content-/vocab-gat, los van convergentie.
- `_wat_geldt_hier` is traag op dichte stadskernen (statement-timeout 500 → bot 5× retry); pre-existing.

## Deploy-volgorde

1. OCD-prod: deploy deze branch (nieuwe endpoints + `vraag`-param).
2. Bot: `BOT_USE_KERNEL` is default-aan; valt veilig terug als OCD-prod nog niet bij is.
3. Viewer: zet `botEngineRetrieval` prod-aan zodra OCD-prod de engine heeft.

## Meetlat

`omgevingsbot.nl/backend/tests/evaluation/`: `run_eval.py` (antwoord, test_cases_v2),
`run_retrieval_eval.py` (deterministisch op bronnen). Laatste cijfers (test_cases_v2, Qwen2.5:14b):
bot eigen rank 74,28% · gedeelde engine ~70-72% (binnen ruis) · pre-gewogen-SKOS 70,62% · pre-SKOS 68,03%.
