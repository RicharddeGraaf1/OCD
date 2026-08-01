# Branch-analyse OCD — 2026-08-01

Vervolg op [branch-consolidatie-2026-07-18.md](branch-consolidatie-2026-07-18.md),
die op belangrijke punten achterhaald is. Doel: van drie zwevende branches naar
één werkelijkheid (`main`), met een onderbouwd besluit over wat er nog aan hangt.

## Uitgangsstand

| Branch | T.o.v. main | Status |
|---|---|---|
| `fix/vergunningen-hot-path` | was identiek aan main | naam-restant, kan weg |
| `feat/rp-planvoorraad` | 63 achter, 1 vooruit | **verwijderd** — feature stond al op main |
| `feat/retrieval-kernel-convergentie` | 63 achter, 34 vooruit | **0 unieke commits** t.o.v. vector-chunk-lagen |
| `feat/vector-chunk-lagen` | 63 achter, 44 vooruit | de enige met unieke inhoud |

De julinotitie stelde dat main achterliep op productie. Dat klopt niet meer:
`wro_imro2006.py`, `wro_planvoorraad.py`, `vergunningen.py` (inclusief het
doorlooptijd-endpoint, dat in `vergunningen.py` zit en niet in `main.py` — daar
zoeken geeft een vals negatief), de BOPA-loader en de schone vector-index staan
allemaal op main.

## Draait productie de kernel? Nee.

De branch voegt drie routes toe die main niet heeft. Die zijn op de
productie-API afwezig, terwijl twee routes die **alleen** op main bestaan er wél
zijn:

| Route | Herkomst | `ocd-api-production` |
|---|---|---|
| `/v1/hertaling/lookup` | alleen main | aanwezig |
| `/v1/mer/trajecten` | alleen main | aanwezig |
| `/fast-path` | alleen branch | afwezig |
| `/v1/rank` | alleen branch | afwezig |
| `/v1/vraag-op-locatie` | alleen branch | afwezig |

Gemeten via `GET /openapi.json` op 2026-08-01. Conclusie: **productie draait de
main-lijn.** De retrieval-kernel is nooit gedeployed. Daarmee vervalt de zorg uit
de julinotitie dat main "niet deploybaar" zou zijn.

## Wat er op `feat/vector-chunk-lagen` staat

Uniek op de branch:

| Bestand | Regels | Wat |
|---|---|---|
| `ocd-api/intent.py` | 89 | intentieherkenning op de vraag |
| `ocd-api/rank.py` | 112 | herrangschikking van regelteksten |
| `ocd-api/fastpaths.py` | 62 | directe paden voor herkenbare vraagvormen |
| `ocd-api/keywords.py` | +17 | uitbreiding op de main-versie |
| `ocd-api/viewer_golden_set.json` | 4.239 | gouden set voor viewer-retrieval |
| `ocd-api/tools/kernel_retrieval_eval.py` | 107 | eval-harnas |
| `ocd-api/tools/build_viewer_golden_set.py` | 135 | generator voor die set |
| `ocd-api/RETRIEVAL-CONVERGENTIE.md` | 67 | ontwerpnotitie |
| 4 losse `poc_*`/`deploy_*`-scripts + 3 docs | — | grotendeels achterhaald |

De kernel zelf is dus **263 regels**. Het klinkt als weinig, maar de aanroepen
zitten verspreid over zes modules: `main.py`, `semantisch.py`,
`regelteksten_bij_vraag.py`, `antwoord_bij_vraag.py`, `vergunningen.py` en
`keywords.py`. Precies die modules zijn op main sindsdien doorontwikkeld —
`main.py` verschilt ~900 regels tussen de twee kanten.

## Waarom niet mergen

De branch loopt 63 commits achter en mist alles van na 9 juli: de
sync-actualiteit, `mv_regel_op_locatie`, `ala_punt`, de hertaling-cache, de
geo-health-view, de hide-first-audit, de vergunningen-hot-path, en al het werk
van vannacht. Een merge is dus geen "kernel erbij", maar een driewegs-integratie
in het hart van de API, met conflicten in precies de modules die het zwaarst zijn
doorontwikkeld — om code binnen te halen die **nooit in productie heeft gedraaid**
en waarvan het nut niet gemeten is tegen de huidige main.

## Advies

1. **Oogst de test-assets nu.** `viewer_golden_set.json` en de twee tools onder
   `ocd-api/tools/` zijn waardevol los van de kernel: een gouden set voor
   viewer-retrieval is bruikbaar om *de huidige* main te meten. Cherry-pick die
   drie bestanden naar main; ze raken geen productiecode.
2. **Behandel de kernel als ontwerp, niet als code.** `RETRIEVAL-CONVERGENTIE.md`
   plus 263 regels zijn beter opnieuw af te leiden tegen de huidige main dan te
   porten door zes gedivergeerde modules heen. Meet eerst met de gouden set of
   main's retrieval tekortschiet op de assen die de kernel adresseerde; pas dan
   bouwen.
3. **Archiveer de branch als tag** (`archief/vector-chunk-lagen`) en verwijder de
   branch-pointers. Een tag bewaart alles permanent zonder dat de branchlijst
   suggereert dat er nog iets loopt. `feat/retrieval-kernel-convergentie` kan
   direct weg — nul unieke commits.
4. **Verwijder `fix/vergunningen-hot-path`**; die wees naar de oude main-tip.

Na 1–4 is er één branch (`main`), één tag als archief, en één openstaande
inhoudelijke vraag (is de kernel nodig?) die met data te beantwoorden is in
plaats van met een merge.
