# Objecten + Regels retrieve-endpoints — conceptplan

**Datum**: 2026-05-17
**Status**: conceptueel ontwerp, ter beoordeling vóór implementatie
**Companion**:
- `C:\GIT\omgevingsbot.nl\docs\20260510_uniform-zoektermen-contract.md` — voorganger-contract dat nu vervangen wordt
- `C:\GIT\OCD\docs\brondocumenten-coverage-gap.md` — losse cases die dit ontwerp deels oplost
- `C:\GIT\OCD\docs\imtr-activiteit-koppeling.md` — niet binnen scope; raakt activiteit-rangschikking marginal

## Wat dit voorstelt

Een herontwerp van de retrieval-laag tussen omgevingsbot en OCD. De huidige zes object-/regel-endpoints (`/v1/normwaarde`, `/v1/activiteit`, `/v1/bestemming`, `/v1/onderwerp`, `/v1/regeltekst`, plus aggregator `/v1/adres`/`/v1/locatie` voor regel-content) worden vervangen door drie generieke endpoints:

1. **`/v1/keywords/extract`** (bestaat, uitbreiden) — vraag → gerangschikte trefwoorden met relevantie-scores
2. **`/v1/objecten`** (nieuw) — locatie + trefwoorden → gerangschikte IMOW-objecten (alle objecttypes verenigd)
3. **`/v1/regels`** (nieuw, refactor van `/v1/regeltekst`) — locatie + trefwoorden → gerangschikte regelteksten met gewogen scoring

De bot roept alle drie achter elkaar aan, bouwt een gestructureerde JSON-payload en geeft die aan Mistral. **Geen detector-regexes meer, geen pad-keuze in de orkestratie.** Path A blijft als optimalisatie: als het topscore-object in `/v1/objecten` een scherpe kwalificatie + `score ≥ drempel` heeft, antwoordt de bot deterministisch zonder LLM-call.

## Waarom

Drie chronische zwakke plekken die door één ontwerp tegelijk worden geadresseerd:

1. **Detector-fragility** — `_NORM_PATTERNS` en `_ACTIVITEIT_QUESTION_PATTERN` missen om de haverklap (R20 "hoe diep", R21 "grondwater onttrekken voor besproeien"). Per gemist patroon zakt de hele vraag naar regular flow met 6000-char blob.
2. **Pad-detectie-risico** — fout pad = Mistral krijgt context die niet bij de vraag past. Dubbele fout: classifier mist én eindgebruiker krijgt onjuist antwoord.
3. **Lexicale gaten** — "kapsalon" matcht niet "beroep aan huis"; huidige expand_with_synonyms + losse SKOS-extractie vangen onvoldoende.

Het nieuwe ontwerp zegt: **de retrieval-laag is verantwoordelijk voor relevantie, niet de orkestratie**. Bot stelt vraag, OCD vertelt wat belangrijk is, Mistral synthetiseert.

## Wat blijft, wat gaat weg

**Blijft (uitgebreid of refactored)**:
- `/v1/keywords/extract` — uitbreiding
- `/v1/regels` — refactor van `/v1/regeltekst`
- `/v1/objecten` — nieuw
- `/v1/adres` en `/v1/locatie` — afgeslankt tot **alleen locatie-resolutie** (adres/perceel → RD-coord). Regel-retrieval-functionaliteit verdwijnt eruit.
- `/v1/gezagen`, `/v1/overzicht` — admin/metadata, ongewijzigd

**Verwijderd** (legacy):
- `/v1/normwaarde`
- `/v1/activiteit`
- `/v1/bestemming`
- `/v1/onderwerp`
- `/v1/regeltekst` (functionaliteit gaat naar `/v1/regels`)

**Aan bot-kant verwijderd** (`omgevingsbot.nl/backend`):
- `_NORM_PATTERNS` + `_detect_norm_question`
- `_ACTIVITEIT_QUESTION_PATTERN` + `_detect_activiteit_question` + `_ACTIVITEIT_STOPWORDS`
- `_detect_bestemming_question`
- Hele Path-A/B/Regular keuze-tree in `process_message_ocd`
- `_build_regular_dso_context`
- `_build_focused_norm_context` + `_build_focused_activiteit_context`
- Bucket-logica (`match_bucket = 'detector'/'keyword'`) in ocd_service.py
- L1 `state_summary.norm_buckets` / `activiteit_buckets` — vervangen door `objecten_top_score`, `regels_top_score`

**Aan bot-kant behouden**:
- `_format_norm_answer` + `_format_activiteit_answer` — aangeroepen bij score-drempel-hit (Path A-equivalent)
- `inspect_ocd_pipeline` — refactored om nieuwe endpoints aan te roepen
- L1/L2 eval-infra ongewijzigd in structuur

## Endpoint-specificaties

### 1. `/v1/keywords/extract` (uitgebreid)

**Bestaat al** met `matched_concepts` zonder relevantie-ranking.

**Nieuwe vorm**:

```
POST /v1/keywords/extract
{
  "question": "Wat is hier de maximale bouwhoogte?",
  "max_concepts": 10,
  "include_broader": true
}
```

**Response**:

```
{
  "keywords": [
    {"term": "bouwhoogte",          "relevantie": 1.00, "bron": "letterlijk"},
    {"term": "maximale bouwhoogte", "relevantie": 0.95, "bron": "letterlijk-phrase"},
    {"term": "max",                 "relevantie": 0.80, "bron": "letterlijk"},
    {"term": "hoogte",              "relevantie": 0.70, "bron": "skos-broader"},
    {"term": "gebouw",              "relevantie": 0.50, "bron": "skos-related"}
  ],
  "matched_concepts": [
    {"uri": "...", "naam": "Maximale bouwhoogte", "relatie": "exact"}
  ]
}
```

**Relevantie-bepaling** (kalibreerbaar, initieel):

| Type | Gewicht | Voorbeeld |
|---|---|---|
| Letterlijk woord uit vraag (≥4 chars) | 1.00 | "bouwhoogte" |
| Letterlijke multi-word-fragment | 0.95 | "maximale bouwhoogte" |
| Korte vraagwoorden (3 chars) | 0.80 | "max" |
| Synoniem uit `expand_with_synonyms` | 0.85 | "houtopstand" voor "boom" |
| SKOS exact-match concept-naam | 0.80 | "Maximale bouwhoogte" |
| SKOS broader (1 stap omhoog) | 0.60 | "hoogtemaat" |
| SKOS narrower (1 stap omlaag) | 0.55 | "nokhoogte" voor "hoogte" |
| SKOS related | 0.40 | "goothoogte" |

**Open punt 1**: gewichten zijn nattevingerwerk. Kalibreren tegen de 40-case testset.

### 2. `/v1/objecten` (nieuw)

Verenigt de 4 huidige object-endpoints. Eén bak, één scoring-systeem.

**Request**:

```
GET /v1/objecten
  ?x=154799.0&y=466865.0
  &keywords=bouwhoogte:1.00
  &keywords=hoogte:0.70
  &keywords=gebouw:0.50
  &min_score=0.5
  &limit=20
  &include_types=normwaarde,activiteit,bestemming,gebiedsaanwijzing
```

Trefwoorden als `term:gewicht`-paren (repeated). `min_score` filtert; `include_types` is optioneel (default: alle).

**Response**:

```
{
  "x": 154799.0, "y": 466865.0,
  "count": 5,
  "matches": [
    {
      "type": "normwaarde",
      "score": 2.45,
      "matched_keywords": [
        {"term": "bouwhoogte", "veld": "norm_naam", "gewicht_bijdrage": 1.00},
        {"term": "hoogte",     "veld": "norm_naam", "gewicht_bijdrage": 0.70},
        {"term": "gebouw",     "veld": "regeltekst_excerpt", "gewicht_bijdrage": 0.25}
      ],
      "object": {
        "norm_naam": "maximale bouwhoogte",
        "kwantitatieve_waarde": 14,
        "eenheid": "meter",
        "regeling": "Omgevingsplan gemeente Amersfoort",
        "artikel": "Beoordelingsregel hoofdgebouw - bouwhoogte"
      }
    },
    {
      "type": "activiteit",
      "score": 1.80,
      "matched_keywords": [...],
      "object": {
        "activiteit_naam": "Bouwen",
        "kwalificatie": "vergunningplicht",
        "regeling": "...",
        "artikel": "Artikel 5.9"
      }
    }
  ]
}
```

**Scoring-formule** (initieel):

```
score(object) = Σ over alle (term, veld)-matches:
    keyword.relevantie × veld_gewicht[veld] × match_strength
```

Veld-gewichten:

| Veld | Gewicht | Reden |
|---|---|---|
| `naam` / `norm_naam` / `activiteit_naam` | 1.00 | exacte naam = sterk signaal |
| `kwalificatie` / `kwalitatieve_waarde` | 0.70 | structurele label |
| `groep` | 0.50 | thematische categorie |
| `regeltekst_excerpt` (FTS) | 0.30 | content-match, zwakker |
| `artikel` / `artikel_wid` | 0.20 | meestal toevallig |

`match_strength`: 1.0 voor exact substring (ILIKE), 0.7 voor FTS-phrase, 0.5 voor FTS-token.

**Open punt 2**: veld-gewichten op testset kalibreren.

### 3. `/v1/regels` (refactor van `/v1/regeltekst`)

**Request**:

```
GET /v1/regels
  ?x=154799.0&y=466865.0
  &keywords=bouwhoogte:1.00
  &keywords=hoogte:0.70
  &min_score=0.4
  &limit=10
```

**Response**:

```
{
  "matches": [
    {
      "score": 1.45,
      "regeling": "Omgevingsplan gemeente Amersfoort",
      "artikel": "Beoordelingsregel hoofdgebouw - bouwhoogte",
      "artikel_wid": "...art_5.9",
      "regeltekst_excerpt": "Voor het hoofdgebouw geldt een maximale bouwhoogte van 14 meter...",
      "matched_keywords": [
        {"term": "bouwhoogte", "match_strength": 1.0},
        {"term": "hoogte",     "match_strength": 0.7}
      ]
    }
  ]
}
```

**Scoring-formule** (initieel, kalibreerbaar):

```
score(regel) = ts_rank(FTS_vector, FTS_query) × 0.5
             + Σ over matched terms: keyword.relevantie × match_strength × 0.5
```

Hybride: FTS-rank voor recall + weighted keyword-match voor precisie.

**Open punt 3**: balans tussen FTS-gewicht en keyword-gewicht; initieel 50/50.

### 4. `/v1/adres` en `/v1/locatie` — afgeslankt

Behouden voor adres → RD-coord en RD-coord → adres-display-info. Verlies van de huidige regel-retrieval-functionaliteit. De bot doet die nu via deze endpoints; na herontwerp roept de bot expliciet `/v1/objecten` + `/v1/regels` aan.

**Nieuwe response-vorm** (alleen locatie-info, geen regel-content):

```
GET /v1/adres?q=Boldershof+32+3811GN+Amersfoort

{
  "adres": "Boldershof 32, 3811GN Amersfoort",
  "rd_x": 154799.0,
  "rd_y": 466865.0,
  "bronhouders": ["gm0307"],   // welke gezagen actief op deze locatie
  "weergavenaam": "Boldershof 32, 3811GN Amersfoort"
}
```

## Multi-word concept-handling

**Open punt 4**: hoe omgaan met multi-word concepten als "kapsalon aan huis"?

Voorstel:
1. `/v1/keywords/extract`: SKOS-concepten met multi-word namen komen als ÉÉN trefwoord met phrase-vorm
2. `/v1/objecten` en `/v1/regels`: phrase-matches scoren hoger (`match_strength` 1.0) dan token-matches (`match_strength` 0.5)
3. Als hele phrase niet matcht, fallback op individuele tokens met lager gewicht

Kalibreren op R08 (winkel begin), R15 (dakopbouw), R34 (hoogwaterbescherming) — daar zijn multi-word concepten cruciaal.

## Bot-side gebruik

Drastische vereenvoudiging:

```python
async def process_message_v7(location, question):
    # 1. Locatie naar RD-coord
    loc = await ocd.adres(q=location)  # of locatie(x=, y=) bij RD-input
    rd_x, rd_y = loc["rd_x"], loc["rd_y"]

    # 2. Trefwoorden
    keywords_resp = await ocd.keywords_extract(question)
    keywords = keywords_resp["keywords"]  # gerangschikt met relevantie

    # 3. Parallelle queries
    objecten, regels = await asyncio.gather(
        ocd.objecten(rd_x, rd_y, keywords, min_score=0.5, limit=20),
        ocd.regels(rd_x, rd_y, keywords, min_score=0.4, limit=10),
    )

    # 4. Path-A fast-track: deterministisch antwoord als topscore-object scherp is
    top_obj = objecten["matches"][0] if objecten["matches"] else None
    if top_obj and top_obj["score"] >= 2.0:
        if top_obj["type"] == "normwaarde" and top_obj["object"].get("kwantitatieve_waarde"):
            return _format_norm_answer([top_obj["object"]], ..., location)
        if top_obj["type"] == "activiteit" and top_obj["object"].get("kwalificatie") in SCHERPE_KWALIFICATIES:
            return _format_activiteit_answer([top_obj["object"]], ..., location)

    # 5. JSON-payload naar LLM
    payload = build_payload(question, loc, keywords, objecten, regels)
    return await llm_synthesize(payload)
```

Geen pad-detectie, geen `_NORM_PATTERNS`, geen bucket-logica. Score-drempels doen het werk.

## JSON-payload naar Mistral

```
{
  "vraag": "Wat is hier de maximale bouwhoogte?",
  "locatie": {"adres": "...", "rd_x": 154799, "rd_y": 466865},
  "trefwoorden_gebruikt": [
    {"term": "bouwhoogte", "relevantie": 1.00},
    {"term": "hoogte",     "relevantie": 0.70}
  ],
  "relevante_objecten": [
    {"type": "normwaarde", "score": 2.45, "object": {...}}
  ],
  "relevante_regels": [
    {"score": 1.45, "regeling": "...", "artikel": "...", "excerpt": "..."}
  ],
  "geen_resultaat_voor": []
}
```

Mistral-prompt:
> *Beantwoord de vraag op basis van de eerste niet-lege bron, in deze volgorde: `relevante_objecten` (begin met hoogste score) → `relevante_regels`. Citeer alleen artikel- en regelnamen die in de payload staan. Antwoord in 2-3 zinnen.*

Met JSON-output-mode geforceerd in Mistral (Ollama `format: "json"`) krijgt antwoord óók een schema.

## Hergebruik-analyse per endpoint

| Endpoint-onderdeel | Bestaande infra | Inspanning nieuw |
|---|---|---|
| `/v1/keywords/extract` scoring + bron-tags | SKOS-lookup + `include_broader` | Output-laag uitbreiden met relevantie + tags |
| `/v1/objecten` verenigde scoring | 4 losse SQL-queries (norm/act/best/onderw) | Optie B: Python-aggregator over de 4. Optie A: één UNION-SQL |
| `/v1/regels` gewogen FTS | `ts_rank`-machinerie in `/v1/regeltekst` | Per-trefwoord-weging + min_score-filter |
| Locatie-resolutie | `/v1/adres` + `/v1/locatie` doen al adres→RD | Afslanken: regel-content eruit |
| OCD-side `ocd_artikel_label()` | Al toegevoegd (V6.19, 2026-05-17) | Hergebruik in nieuwe queries |

## Fasering

| Fase | Werk | Duur |
|---|---|---|
| 1. Kalibratiebasis | Per case (5-10 stuks) "ideale" objecten + regels documenteren | 1 dag |
| 2. `/v1/keywords/extract` uitbreiden | Scoring + bron-tags + multi-word phrase | 2 dagen |
| 3. `/v1/regels` refactor | Gewogen FTS + min_score | 1-2 dagen |
| 4. `/v1/objecten` (optie B: Python-aggregator) | Aggregator + scoring | 3 dagen |
| 5. `/v1/adres` + `/v1/locatie` afslanken | Verwijder regel-content uit response | 1 dag |
| 6. Bot-orchestratie herschrijven | Pad-detectie weg, score-drempel + payload-bouwer | 2 dagen |
| 7. Legacy endpoints verwijderen | `/v1/normwaarde`, `/v1/activiteit`, `/v1/bestemming`, `/v1/onderwerp`, `/v1/regeltekst` weg uit `main.py`; SQL-functies opruimen | 1 dag |
| 8. Kalibratie + L1/L2-validatie | Gewichten tunen, drempels zoeken | 2 dagen |
| 9. (Optioneel later) `/v1/objecten` optie A | UNION-SQL als clean-up | 2 dagen |
| **Kerntraject (1-8)** | | **~12-13 dagen** |

**Parallelisatie**: fase 2, 3, 5 kunnen tegelijk met fase 4. Met twee mensen tegelijk valt het naar ~1 week kerntraject.

**Volgorde voor één persoon**:
1. Fase 1 (kalibratiebasis) eerst — geeft ground-truth voor alle volgende stappen
2. Fase 2 + 3 + 5 als rij (alle drie raken verschillende endpoints)
3. Fase 4 (`/v1/objecten`) als grootste blok
4. Fase 6 (bot-orchestratie) — kan pas als 2+3+4 staan
5. Fase 8 (kalibratie) — sluit het traject af
6. Fase 7 (legacy-removal) — uitvoerbaar zodra fase 6 productie is

## Acceptatiecriteria

| Fase | Criterium |
|---|---|
| 1 | Voor 10 cases ground-truth gedocumenteerd in `tests/calibration_ground_truth.json` |
| 2 | Voor elke testcase geeft `/v1/keywords/extract` ≥1 trefwoord met `relevantie ≥ 0.8` dat semantisch klopt |
| 3 | Voor elk Path-B-case (R28-R32) staat de juiste artikel-regel in top-3 van `/v1/regels` |
| 4 | Voor elk Path-A-case (R01-R03, R22) staat het juiste object op #1 met `score ≥ 2.0` |
| 5 | `/v1/adres?q=...` retourneert alleen locatie-info, geen regelteksten/bestemmingen |
| 6 | L1-eval: minimaal evenveel PASS als V6.19 (20), met dezelfde testset |
| 7 | `grep -r "v1/normwaarde\|v1/activiteit\|v1/onderwerp\|v1/regeltekst"` in beide repos geeft 0 hits |
| 8 | L2-eval: avg score ≥ 65% (was 53% V6.19, 60.2% V6.18-baseline) |

## Wat dit niet oplost

- **Brondocumenten-coverage** (Groep A in `brondocumenten-coverage-gap.md`): R26/R27 Alde Feanen, R37/R38 Ontwerp Nota Ruimte, R39 Gaaf Gelderland — geen retrieval-fix lost iets op dat niet ingest is
- **Ambtsgebied-geo-koppeling** (Groep B): R34, R35, R36, R40 — Programma's/Projectbesluiten missen `activiteit_locatieaanduiding`. Vereist join-fix in locatie-resolver, los van dit ontwerp
- **IMTR-vraagboom voor "anders geduid"-cases** (R15, R18, R19): wacht op `imtr-activiteit-koppeling.md`-fixes in de loader

Deze drie zijn data-engineering werkpakketten die parallel kunnen lopen aan dit retrieval-herontwerp.

## Open punten — kalibratie-checklist

Vier punten waar nattevinger-getallen staan; op de testset toetsen:

1. **Trefwoord-relevantie-gewichten** (`letterlijk=1.0, broader=0.6, related=0.4`) — verfijnen tot trefwoord-orde op 40 cases klopt
2. **Object-scoring veld-gewichten** (`naam=1.0, kwalificatie=0.7, excerpt=0.3`) — toetsen of de juiste objecten bovenaan komen
3. **Max-count voor trefwoord-set** (initieel 10) — kijken of dat genoeg recall geeft zonder ruis
4. **Multi-word-phrase-handling** — phrase-gewicht vs token-fallback-gewicht tunen

Documenteer kalibratie-keuzes met *waarom*-onderbouwing per gewicht, niet alleen het getal — zodat ze later herzien kunnen worden als de testset verandert.
