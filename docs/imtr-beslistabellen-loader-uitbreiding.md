# Plan — IMTR-beslistabellen in de loader (i2a uitvuldatie)

**Datum:** 2026-06-17
**Status:** ontwerp, klaar voor go/no-go
**Aanleiding:** de toepasbare-regel-checker (TRCG) heeft een *uitvoerbaar*
bronhoudermodel nodig om de toepasbare regel tegen de regeltekst af te zetten
(outcome-equivalentie). Dat model is met de huidige loader niet uit `i2a` te
reconstrueren.

---

## 1. Het probleem in één zin

`imtr_loader._parse_and_store_dmn` **downloadt de volledige DMN-XML** (via
`/toepasbareRegels/{id}/sttrBestand`) maar bewaart er **alleen de element-namen**
van: het gooit de beslistabellen, hit policies, condities en afhankelijkheden weg.

Concreet (zie [`src/loaders/imtr_loader.py`](../dso-loader/src/loaders/imtr_loader.py)
regels 190-243):

| Wat de XML bevat | Wat de loader nu opslaat |
|---|---|
| `semantic:decision` (naam + id) | ✅ `i2a.dmn_element` (element_type=Decision) |
| `semantic:inputData` (naam + id + typeRef) | ✅ naam; ❌ typeRef |
| `semantic:decisionTable/@hitPolicy` | ❌ |
| `semantic:rule` + `inputEntry`/`outputEntry` (FEEL) | ❌ |
| `semantic:informationRequirement` (edges) | ❌ |
| `uitv:uitvoeringsregel` (vraag/rekenRegel) | ⚠️ alleen `regel_type` |

Geverifieerd op een echte aanlevering (vendored sample
`Outcome - AfscheidingDakterras.xml`): 32× `decisionTable`, 41 `rule`, 80
`inputEntry`, 41 `outputEntry`, 25 `informationRequirement`, hit policies
UNIQUE/ANY/COLLECT. Allemaal aanwezig in de XML, allemaal weggegooid.

**De winst:** geen nieuwe API-call nodig. De XML komt al binnen. Uitbreiden =
méér parsen uit bytes die we al hebben + opslaan.

---

## 2. Hergebruik: de parser bestaat al

TRCG heeft de DMN-XML → graaf-reductie al geport en getest
(`trcg/dmn/reduce.py`, roundtrip-getest tegen 99 productiemodellen, dezelfde
`semantic:`-namespace). De loader-uitbreiding hoeft die logica niet opnieuw te
bedenken — alleen de *opslag* moet erbij. Twee opties hoe we dat delen:

- **A. Code dupliceren** in `imtr_loader` (lxml, zelfde XPaths). Geen
  cross-repo-dependency, maar twee plekken onderhouden.
- **B. De reductie als bron-van-waarheid** nemen: loader produceert dezelfde
  `{external_variables, nodes, edges, hit_policy, logic}`-structuur en slaat die op.

Aanbevolen: **B qua *vorm*, A qua *code*** — dezelfde JSON-vorm produceren
(zodat TRCG hem 1-op-1 kan inlezen), maar de ~40 regels parse-logica in de loader
zelf houden (OCD heeft geen TRCG-dependency). De vorm is het contract, niet de code.

---

## 3. Opslagkeuze (trade-off)

| Aspect | B1. JSONB-graaf per regelbestand | B2. Relationeel uitgesplitst |
|---|---|---|
| DDL-werk | 1 kolom + 3 health-scalars | 2 kolommen + 4 tabellen |
| Loader-werk | reduce → `json.dumps` → 1 insert | per regel/conditie/edge inserts |
| TRCG leest het | `DecisionGraph.model_validate_json(kolom)` direct | graaf-builder die joint |
| Per-regel queryen ("welke regels met drempel X") | nee (blob) | ja |
| Past bij data-health-laag (audit per kolom) | matig (blob) | goed |
| Risico | laag (reuse bewezen reductie) | hoger (meer mapping) |

**Aanbeveling: B1 (JSONB) voor v1**, met enkele afgeleide scalars voor de
health-laag. Reden: de consument (TRCG) wil de *hele graaf*, niet losse regels;
de reductie is al bewezen; dit deblokkeert de checker met minimaal risico. B2 is
de v2-stap zodra er een concrete per-regel-queryvraag ontstaat (bv. een
drempelwaarde-audit los van de tekst).

### DDL (v1)

```sql
ALTER TABLE i2a.toepasbaar_regelbestand
  ADD COLUMN IF NOT EXISTS beslisgraaf      JSONB NULL,  -- volledige niveau-B graaf
  ADD COLUMN IF NOT EXISTS aantal_decisions INT  NULL,
  ADD COLUMN IF NOT EXISTS aantal_regels    INT  NULL,   -- som van decisionTable-rules
  ADD COLUMN IF NOT EXISTS heeft_logica     BOOLEAN NOT NULL DEFAULT FALSE;
```

`beslisgraaf` bevat exact de structuur die `trcg.dmn.DecisionGraph` verwacht
(`type`, `top_node`, `external_variables`, `nodes` met `logic`+`hit_policy`,
`edges`). De drie scalars zijn de audit-ankers (sectie 6).

---

## 4. Loader-wijziging

Vervang `_parse_and_store_dmn` (de naam-only versie) door een variant die:

1. **inputData** → `external_variables` met `type` uit `@typeRef`
   (`boolean` als "boolean" in typeRef, anders `string`).
2. **decision** → `nodes` met `@hitPolicy` van de `decisionTable`.
3. **rule** → per `inputEntry` een conditie (kolom = `inputExpression/text`,
   waarde = FEEL-cel), per `outputEntry` de uitkomst → `logic[].when/then`.
4. **informationRequirement** → `edges` (`requiredDecision`/`requiredInput` href
   → doel-decision), met id→naam-resolutie zoals in `reduce.py`.
5. **top_node** bepalen: de decision die geen bron is van een edge (sink), bij
   voorkeur de naam die het modeltype bevat — identieke heuristiek als `reduce.py`.
6. De graaf serialiseren naar `beslisgraaf` + scalars vullen + één UPDATE op
   `toepasbaar_regelbestand`.

De FEEL-cel-parse (booleans, `>=`/`<=`-operatoren, `contains()`, `not(null)`)
is 1-op-1 over te nemen uit `reduce.parse_feel_cell` / `executor.eval_condition`.
**Behoud de huidige `uitvoeringsregel`-insert** — die is complementair (de
interactieve vraag/rekenregel-laag) en raakt los van de beslistabel.

Geen wijziging aan de fetch-laag, paginering of rate-limiting.

---

## 5. Fasering

| Fase | Inhoud | Klaar als |
|---|---|---|
| **0. Spike (½ dag)** | Download 3-5 echte `sttrBestand` uit de live API, draai `reduce.py` erop, inspecteer de grafen. | Bevestigd dat live-XML == vendored sample-structuur (zelfde `semantic:decisionTable`/`rule`). **Go/no-go-poort.** |
| **1. DDL (¼ dag)** | `ALTER TABLE` + migratie (drop/reload imtr; OCD is pre-productie voor dit schema-deel). | Kolommen bestaan, bestaande loads ongebroken. |
| **2. Loader (1 dag)** | `_parse_and_store_dmn` uitbreiden + unit-test op een vendored XML (graaf-output == bijbehorende gold-JSON). | `imtr`-load vult `beslisgraaf` + scalars; coverage gemeten. |
| **3. TRCG-zijde (½ dag)** | `i2a_bridge` krijgt `executable_graph_from_i2a(ns)` die `beslisgraaf` inleest; `check_tegen_bronhouder` draait nu écht. | Outcome-score op een echte gemeente. |
| **4. Health (¼ dag)** | Coverage-metric + audit-anker in de data-health-laag. | `% regelbestanden met heeft_logica` zichtbaar in `/v1/data-health`. |

Totaal ~2,5 dag. Spike eerst — die bepaalt of fase 1-4 doorgaan.

---

## 6. Data-health & audit-anker

Sluit aan op de staande data-health-laag (`mv_bronhouder_health`, `v_data_health`):

- **Nieuwe metric**: `aandeel_regelbestanden_met_logica` =
  `COUNT(*) FILTER (WHERE heeft_logica) / COUNT(*)` over
  `i2a.toepasbaar_regelbestand`.
- **Audit-anker** (analoog aan "annotatie hol = content-realiteit"):
  > `dmn_element` bestaat maar `beslisgraaf IS NULL` of `heeft_logica = false`
  > terwijl de XML wél decisionTables had → **loader-gap**, geen content-realiteit.
  Een regelbestand dat écht geen beslislogica heeft (alleen InputData) is zeldzaam
  en moet apart geteld worden, niet als gat geïnterpreteerd.
- **Regressie**: voeg de roundtrip (vendored source-XML → graaf == gold-JSON) toe
  als regressietest, zodat een parser-regressie in de loader hard faalt.

---

## 7. Risico's

| Risico | Kans | Mitigatie |
|---|---|---|
| Live `sttrBestand` wijkt structureel af van de vendored sample (andere encoding van de logica dan plain decisionTable) | midden | **Fase 0 spike** vóór alle DDL/loaderwerk |
| FEEL-subset dekt niet alle cellen (value-lists, samengestelde expressies) | midden | Bekende parser-limieten overnemen + loggen welk % cellen op de fallback-tak valt; niet stil afkappen |
| JSONB-blob ondermijnt queryability | laag | scalars erbij voor health; B2-relationeel als v2 bij concrete queryvraag |
| Reload-omvang (alle bronhouders opnieuw) | laag | `imtr`-load is al idempotent (ON CONFLICT); incrementeel per OIN mogelijk |
| Numerieke drempels blijven in UR-bladeren weggeabstraheerd | inherent | buiten scope; aparte `normwaarde`-check (TRCG-doc §6 open vraag 1) |

---

## 8. Relatie met andere trajecten

- **TRCG** ([`ToepasbareRegelCheckerEnGenerator`](../../ToepasbareRegelCheckerEnGenerator/docs/imtr-vorm-automatisering-kwaliteit.md)):
  directe consument; de `beslisgraaf`-kolom is het contract.
- **annotatieconformiteit.nl**: de outcome-afwijkingsscore die hierdoor mogelijk
  wordt, is een nieuwe bronhouder-kwaliteitscategorie — hoort daar thuis, niet in
  de viewer.
- **Bestaande `i2a.uitvoeringsregel`**: blijft; dit plan raakt alleen de
  beslistabel-kant.

---

## 9. Eerste concrete stap

Fase 0-spike-scriptje (geen DDL, geen commit nodig): voor 3-5 regelbestanden de
`sttrBestand` ophalen, door `trcg.dmn.reduce.dmn_xml_to_graph` halen, en
`is_executable` + `is_outcome_testable` + aantal nodes/edges printen. Slaagt dat,
dan go op fase 1.
