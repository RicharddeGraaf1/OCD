# Toepasbare Regel Checker — Ontwerp

**Status:** ontwerp, niet geïmplementeerd
**Datum:** 2026-04-15
**Relatie:** idee #2 uit vault_v1/ideeen.md, spiegelbeeld van idee #1 (generator)

---

## Doel

Per activiteit in een omgevingsplan (of omgevingsverordening,
waterschapsverordening) automatisch controleren of de toepasbare regel
(DMN-beslislogica) overeenkomt met de onderliggende regelgeving
(artikeltekst). Output: een kwaliteitsrapport per gemeente met concrete
afwijkingen en een totaalscore.

---

## Waarom dit waardevol is

Toepasbare regels (STTR/DMN) worden los van de regelgeving beheerd.
Na een wijziging van het omgevingsplan wordt de DMN niet altijd
mee-geüpdatet — of de oorspronkelijke vertaling was al onnauwkeurig.
Er is nu geen geautomatiseerde manier om dit te detecteren.

De checker geeft bevoegde gezagen en plansoftware-leveranciers een
instrument om de kwaliteit van hun toepasbare regels te meten en
gericht te verbeteren.

---

## Databronnen (alles in OCD)

De volledige keten van artikeltekst tot DMN-beslislogica zit in de
OCD-database, verdeeld over twee keten-schema's:

**p2p (artikeltekst-kant):**
```
p2p.activiteit
  → p2p.activiteit_locatieaanduiding
    → p2p.juridische_regel (regel_type, kwalificatie, thema)
      → p2p.tekst_element (artikeltekst via regeltekst_wid)
        → p2p.regeling (opschrift, bronhouder)
```

**i2a (DMN-kant):**
```
i2a.regelbeheerobject (brug: activiteit_id → p2p.activiteit)
  → i2a.toepasbaar_regelbestand (namespace, sttr_versie, geldigheid)
    → i2a.dmn_element (beslisboom-structuur: decisions, inputs, outputs)
      → i2a.uitvoeringsregel (concrete regels: type, activiteit_urn)
```

**Koppeling:** `i2a.regelbeheerobject.activiteit_id` verwijst naar
`p2p.activiteit.identificatie`. Dit is het scharnierpunt.

---

## Pipeline — 5 stappen

### Stap 1: Inventarisatie per gemeente

SQL-query die per activiteit vaststelt of er een toepasbare regel bestaat:

```sql
SELECT
    a.identificatie,
    a.naam AS activiteit,
    r.opschrift AS regeling,
    CASE WHEN rbo.functionele_structuur_ref IS NOT NULL
         THEN 'ja' ELSE 'nee'
    END AS heeft_toepasbare_regel,
    COUNT(DISTINCT te.id) AS aantal_artikelen,
    COUNT(DISTINCT dmn.id) AS aantal_dmn_elementen
FROM p2p.activiteit a
JOIN p2p.activiteit_locatieaanduiding ala
    ON ala.activiteit_id = a.identificatie
JOIN p2p.juridische_regel jr
    ON jr.identificatie = ala.juridische_regel_id
JOIN p2p.tekst_element te
    ON te.wid = jr.regeltekst_wid
JOIN p2p.regeling r
    ON r.frbr_expression = te.regeling_expression
LEFT JOIN i2a.regelbeheerobject rbo
    ON rbo.activiteit_id = a.identificatie
LEFT JOIN i2a.toepasbaar_regelbestand trb
    ON trb.regelbeheerobject = rbo.functionele_structuur_ref
LEFT JOIN i2a.dmn_element dmn
    ON dmn.regelbestand_ns = trb.namespace
WHERE a.identificatie LIKE 'nl.imow-gm0344.%'  -- ← gemeente-filter
GROUP BY a.identificatie, a.naam, r.opschrift,
         rbo.functionele_structuur_ref
ORDER BY heeft_toepasbare_regel DESC, a.naam;
```

Dit geeft drie categorieën:
- **Met toepasbare regel:** kandidaten voor de checker (stap 2-4)
- **Zonder toepasbare regel:** kandidaten voor de generator (idee #1)
- **Met toepasbare regel maar zonder artikeltekst:** dataconsistentie-probleem

### Stap 2: DMN-logica extraheren (gestructureerd)

De DMN-kant is al gestructureerd in OCD. Per toepasbaar regelbestand:

```sql
SELECT
    dmn.dmn_id,
    dmn.element_type,  -- 'decision', 'inputData', 'knowledgeSource', ...
    dmn.naam,
    ur.regel_type,     -- 'conclusie', 'indieningsvereiste', 'maatregelen', ...
    ur.nen3610_id,
    ur.activiteit_urn
FROM i2a.dmn_element dmn
LEFT JOIN i2a.uitvoeringsregel ur
    ON ur.dmn_element_id = dmn.id
WHERE dmn.regelbestand_ns = %s  -- ← namespace van het regelbestand
ORDER BY dmn.parent_id NULLS FIRST, dmn.id;
```

Uit deze data reconstrueer je de beslisboom:
- **Input-variabelen:** `element_type = 'inputData'` — wat wordt gevraagd
  (bv. "hoogte bouwwerk", "afstand tot erfgrens")
- **Beslissingen:** `element_type = 'decision'` — de beslisknopen
- **Conclusies:** `uitvoeringsregel.regel_type = 'conclusie'` — wat is
  het eindoordeel (vergunningplichtig, meldingsplichtig, toegestaan)
- **Indieningsvereisten:** `regel_type = 'indieningsvereiste'` — wat
  moet je aanleveren

Output van deze stap: een gestructureerd object per activiteit:

```json
{
  "activiteit": "Dakkapel plaatsen",
  "inputs": ["hoogte bouwwerk", "afstand tot voorgevel", "beschermd stadsgezicht"],
  "condities": [
    {"input": "hoogte bouwwerk", "operator": "<=", "waarde": "3 meter"},
    {"input": "beschermd stadsgezicht", "operator": "==", "waarde": "nee"}
  ],
  "conclusie": "vergunningvrij",
  "uitzonderingen": [
    {"conditie": "beschermd stadsgezicht == ja", "conclusie": "vergunningplichtig"}
  ]
}
```

### Stap 3: Artikeltekst analyseren (LLM)

De artikeltekst is vrije tekst met juridische structuur. Een LLM
extraheert de regellogica in hetzelfde format als stap 2.

**Input:** de HTML-inhoud van `p2p.tekst_element.inhoud`, ontdaan van
tags. Plus context: gerelateerde artikelen via IntRefs (interne
verwijzingen in de tekst), gebiedsaanwijzingen, normen.

**LLM-prompt:**

```
Je bent een juridisch analist gespecialiseerd in de Omgevingswet.

Analyseer onderstaand artikel uit het omgevingsplan en extraheer ALLE
voorwaarden, drempelwaarden, uitzonderingen en conclusies als
gestructureerde regels.

Let specifiek op:
- Kwantitatieve drempels (hoogte, oppervlakte, afstand, aantallen)
- Kwalitatieve condities (beschermd stadsgezicht, bebouwde kom, etc.)
- Verwijzingen naar andere artikelen (deze bevatten vaak extra condities)
- De juridische conclusie (vergunningplichtig, meldingsplichtig,
  verbod, toegestaan)
- Uitzonderingen op de hoofdregel

Geef je antwoord als JSON in dit format:
{
  "inputs": ["variabele1", "variabele2"],
  "condities": [
    {"input": "variabele", "operator": "<=|>=|==|!=", "waarde": "..."}
  ],
  "conclusie": "vergunningvrij|vergunningplichtig|meldingsplichtig|verbod",
  "uitzonderingen": [
    {"conditie": "...", "conclusie": "..."}
  ],
  "verwijzingen": ["art. X.Y", "art. Z.W"],
  "onzekerheden": ["... (tekst is ambigu over ...)"]
}

ARTIKEL:
---
{artikeltekst}
---

GERELATEERDE NORMEN (indien van toepassing):
{normen_context}
```

**Normen-context:** als er `p2p.norm` + `p2p.normwaarde` records aan
dezelfde `juridische_regel` hangen, worden die als extra context
meegegeven. Veel drempelwaarden staan niet in de artikeltekst maar in
de normwaarde-annotaties.

**Output:** zelfde JSON-structuur als stap 2, maar nu afgeleid uit de
artikeltekst.

### Stap 4: Vergelijking (LLM-ondersteund)

Leg de DMN-extractie (stap 2) naast de artikel-extractie (stap 3) en
detecteer afwijkingen. Dit kan deels regelgebaseerd, deels via LLM.

**Regelgebaseerde checks:**

| Check | Methode |
|---|---|
| Ontbrekende input-variabele | DMN.inputs ∖ Artikel.inputs |
| Extra input-variabele | Artikel.inputs ∖ DMN.inputs |
| Drempel-afwijking | Vergelijk numerieke waarden per gedeelde input |
| Conclusie-mismatch | DMN.conclusie ≠ Artikel.conclusie |
| Ontbrekende uitzondering | Artikel.uitzonderingen ∖ DMN.uitzonderingen |

**LLM-check (voor de subtielere gevallen):**

```
Vergelijk de volgende twee representaties van dezelfde regel.
De eerste komt uit de DMN-beslisboom (toepasbare regel), de tweede
uit de artikeltekst (juridische bron).

DMN:
{dmn_json}

ARTIKEL:
{artikel_json}

Geef per afwijking aan:
- type: ontbrekend | verkeerd | extra | verouderd
- ernst: fout | waarschuwing | opmerking
- beschrijving: wat wijkt af en waarom is dit relevant
- suggestie: hoe de DMN aangepast zou moeten worden

Als de DMN correct de artikeltekst weerspiegelt, zeg dat expliciet.
```

**Afwijking-types en ernst:**

| Type | Ernst | Voorbeeld |
|---|---|---|
| Verkeerde drempel | **Fout** | Artikel: max 3m, DMN: max 4m |
| Ontbrekende conditie | **Fout** | Artikel noemt "beschermd stadsgezicht", DMN checkt dit niet |
| Verkeerde conclusie | **Fout** | Artikel: meldingsplicht, DMN: vergunningvrij |
| Extra conditie in DMN | **Waarschuwing** | DMN checkt iets dat niet in artikel staat |
| Formulerings­verschil | **Opmerking** | "hoogte" vs. "bouwhoogte" — zelfde bedoeling |
| Verwijzing niet gevolgd | **Waarschuwing** | Artikel verwijst naar art. X.Y, DMN negeert die condities |
| Normwaarde niet in DMN | **Fout** | Normwaarde zegt max 10m, DMN heeft geen input "hoogte" |

### Stap 5: Rapportage

Per gemeente een gestructureerd rapport. Twee niveaus:

**Samenvattingsniveau:**

```
═══════════════════════════════════════════════════════
  Toepasbare Regel Checker — Gemeente Utrecht (0344)
  Regeling: Omgevingsplan gemeente Utrecht
  Datum: 2026-04-15
═══════════════════════════════════════════════════════

  Totaal activiteiten:              180
  Met toepasbare regel:             142  (79%)
  Zonder toepasbare regel:           38  (21%)

  Checker-resultaten (142 gecontroleerd):
    ✓ Correct:                       98  (69%)
    ⚠ Waarschuwing:                  31  (22%)
    ✗ Fout:                          13  ( 9%)

  Kwaliteitsscore:                   69%

  Fouten per type:
    Verkeerde drempel:                5
    Ontbrekende conditie:             4
    Verkeerde conclusie:              2
    Normwaarde niet in DMN:           2
```

**Detailniveau (per activiteit met afwijking):**

```
───────────────────────────────────────────────────────
  Activiteit: Dakkapel plaatsen aan de achterzijde
  Artikel: 4.12 lid 3, Omgevingsplan gemeente Utrecht
  Regelbestand: nl.sttr.gm0344.dakkapel-achterzijde-v2
  Status: ✗ FOUT
───────────────────────────────────────────────────────

  Afwijkingen:
  1. [FOUT] Ontbrekende conditie
     Artikel noemt: "mits niet gelegen in beschermd stadsgezicht"
     DMN mist input-variabele "beschermd stadsgezicht"
     → Suggestie: voeg inputData "beschermd stadsgezicht" (boolean) toe
       met extra decision branch naar "vergunningplichtig"

  2. [WAARSCHUWING] Extra conditie
     DMN checkt "afstand tot naburig erf >= 2m"
     Artikel noemt deze conditie niet expliciet
     → Suggestie: verifieer of deze conditie uit een ander artikel komt
       (mogelijk IntRef niet gevolgd)
```

**Outputformaten:**
- CLI-tabel (rich) voor interactief gebruik
- JSON voor programmatische verwerking
- Markdown voor rapportage / delen met opdrachtgever

---

## Architectuur

```
┌─────────────────────────────────────────────────┐
│                 Checker CLI                      │
│   python -m src.cli checker --gemeente 0344     │
└─────────────┬───────────────────────────────────┘
              │
   ┌──────────▼──────────┐
   │   Stap 1: Query     │  OCD (p2p + i2a)
   │   Inventarisatie     │  → activiteiten + DMN + artikelen
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │   Stap 2: DMN       │  Puur data-extractie
   │   Logica extraheren  │  i2a.dmn_element → gestructureerd JSON
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │   Stap 3: Artikel   │  LLM-call per activiteit
   │   Tekst analyseren   │  p2p.tekst_element → gestructureerd JSON
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │   Stap 4: Vergelijk │  Regelgebaseerd + LLM
   │   DMN vs. Artikel    │  → afwijkingen per activiteit
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │   Stap 5: Rapport   │  CLI / JSON / Markdown
   │   Genereer output    │
   └──────────┴──────────┘
```

**LLM-gebruik:** stap 3 en 4 zijn LLM-intensief. Per activiteit
minimaal 1 LLM-call (artikel-extractie), optioneel 2 (vergelijking).
Bij 142 activiteiten = ~142-284 LLM-calls per gemeente.

**Kostenschatting (Claude Sonnet):**
- ~1500 tokens input per call (artikel + prompt)
- ~500 tokens output per call
- 200 calls × 2000 tokens ≈ 400K tokens ≈ ~$1.20 per gemeente
- Alle 342 gemeenten: ~$400 voor een landelijke scan

---

## Beperkingen en mitigaties

| Beperking | Impact | Mitigatie |
|---|---|---|
| LLM-extractie is ~80% nauwkeurig | False positives/negatives | Menselijke review op "fout"-items; validatie tegen normen |
| IntRefs (verwijzingen naar andere artikelen) | Condities uit andere artikelen gemist | IntRefs parsen uit HTML, gerefereerde artikelen als context meegeven |
| Normwaarden staan apart van artikeltekst | Drempels niet in tekst gevonden | `p2p.norm` + `p2p.normwaarde` als extra context meegeven (al in ontwerp) |
| DMN-structuur is complex (geneste decisions) | Moeilijk te flatten naar simpele regels | Hiërarchisch vergelijken; top-level decisions eerst |
| Regelgeving verandert | Rapport is een snapshot | Periodiek herhalen; delta-rapport t.o.v. vorige run |

---

## Synergie met andere ideeën

**Idee #1 (Toepasbare regel generator):**
- Dezelfde LLM-extractiestap (stap 3) is de basis
- De checker vindt fouten, de generator vult gaten
- Samen: een complete toepasbare-regel-lifecycle

**Idee #5 (AI-bot / Omgevingsbot):**
- Checker-output kan als metadata in de bot:
  "Let op: de toepasbare regel voor deze activiteit wijkt af van de
  artikeltekst (checker-score: 60%)"

**OCD keten-schema's:**
- De checker is een schoolvoorbeeld van een cross-keten query (p2p + i2a)
- Bevestigt het nut van de schema-splitsing: je ziet meteen dat je twee
  domeinen vergelijkt

---

## Integratie met odkwaliteit (annotatieconformiteit)

De repo `c:\GIT\odkwaliteit` (OD-Kwaliteit) is een bestaand
scoringssysteem dat 36 annotatierichtlijnen controleert en een gewogen
kwaliteitsscore per omgevingsdocument oplevert. De architectuur is
modulair: elke richtlijn is een Python-class met een `@register`
decorator in `src/odkwaliteit/scoring/rules/`.

### Overlap met de checker

odkwaliteit raakt al aan toepasbare regels:

| Richtlijn | Wat het checkt | Hoe |
|---|---|---|
| R14 | `activiteitregelkwalificatie` consistent met regeltype | Heuristiek op regeltype-ratio's |
| R22 | Activiteiten met toepasbare regels onderaan hiërarchie | RTR-API check op leaf-level |

Maar drie richtlijnen zijn **handmatig (M)** omdat ze inhoudelijke
beoordeling vereisen:

| Richtlijn | Wat het zou moeten checken | Nu |
|---|---|---|
| R11 | Annoteer alleen wat in de tekst staat | Handmatig |
| R13 | Activiteit helder voor viewer + toepasbare regels | Handmatig |
| R23 | Hiërarchieniveau raadplegen | Handmatig |

Dit zijn precies de checks die de checker met LLM-extractie kan
(deels) automatiseren.

### Optie 1: Nieuwe categorie G in odkwaliteit

Voeg een `f_toepasbare_regels.py` toe aan
`src/odkwaliteit/scoring/rules/` met nieuwe richtlijnen:

```python
# src/odkwaliteit/scoring/rules/f_toepasbare_regels.py

@register
class Richtlijn37DmnConditiesCompleet(Rule):
    """Alle condities uit de artikeltekst zijn aanwezig in de DMN."""
    richtlijn_nr = 37
    categorie = "G"

    def evaluate(self, ctx: RegelingContext) -> RuleResult:
        # Per activiteit met toepasbare regel:
        # 1. Haal DMN-logica op (i2a.dmn_element)
        # 2. Haal artikeltekst op (p2p.tekst_element)
        # 3. LLM-extractie van condities uit artikeltekst
        # 4. Vergelijk: DMN.condities ⊇ Artikel.condities?
        ...

@register
class Richtlijn38DmnDrempelsCorrect(Rule):
    """Drempelwaarden in DMN komen overeen met artikel + normwaarden."""
    richtlijn_nr = 38
    categorie = "G"
    ...

@register
class Richtlijn39DmnConclusieCorrect(Rule):
    """DMN-conclusie (vergunningplichtig/meldingsplichtig/etc.)
    komt overeen met de juridische regel."""
    richtlijn_nr = 39
    categorie = "G"
    ...

@register
class Richtlijn40DmnGeenExtraCondities(Rule):
    """DMN bevat geen condities die niet in de artikeltekst staan."""
    richtlijn_nr = 40
    categorie = "G"
    ...

@register
class Richtlijn41NormwaardenInDmn(Rule):
    """Normwaarden (p2p.normwaarde) die aan dezelfde juridische regel
    hangen zijn als input-variabele aanwezig in de DMN."""
    richtlijn_nr = 41
    categorie = "G"
    ...

@register
class Richtlijn42ToepasbarRegelAanwezig(Rule):
    """Activiteiten met vergunning-/meldingsplicht hebben een
    toepasbare regel (regelbeheerobject aanwezig)."""
    richtlijn_nr = 42
    categorie = "G"
    ...
```

**Weging in `config.py`:**

Huidige gewichten (A=10%, B=15%, C=35%, D=20%, E=15%, F=5%).
Met categorie G erbij:

| Categorie | Oud | Nieuw |
|---|---|---|
| A (Regelingsgebied) | 10% | 8% |
| B (Werkingsgebied) | 15% | 12% |
| C (Activiteiten) | 35% | 28% |
| D (Gebiedsaanwijzing) | 20% | 16% |
| E (Normen) | 15% | 12% |
| F (Thema's) | 5% | 4% |
| **G (Toepasbare regels)** | — | **20%** |

Categorie G krijgt 20% omdat het een hoog-impact check is (directe
burger-consequentie: foutieve vergunningcheck).

### Optie 2: Handmatige regels upgraden naar heuristiek

De LLM-extractie uit de checker kan R11 en R13 deels automatiseren.
Hoe:

**R11 ("Annoteer alleen wat in tekst staat") — nu handmatig:**

Huidige status: "M" (handmatig). De richtlijn controleert of annotaties
(activiteiten, gebiedsaanwijzingen) daadwerkelijk in de artikeltekst
voorkomen, of dat ze "erbij verzonnen" zijn.

Upgrade: gebruik de LLM-extractie (stap 3 van de checker) om uit de
artikeltekst een lijst van activiteiten/condities te halen. Vergelijk
die met de IMOW-annotaties. Als een annotatie niet herleidbaar is tot
de tekst → `voldoet_niet`.

```python
# Pseudo-code voor R11 upgrade
def evaluate(self, ctx):
    for activiteit in ctx.activiteiten:
        artikel_tekst = get_artikel_for_activiteit(activiteit)
        llm_extracted = llm_extract_activities(artikel_tekst)
        if activiteit.naam not in llm_extracted:
            add_bevinding(f"Activiteit '{activiteit.naam}' niet herleidbaar "
                          f"tot artikeltekst")
            status = VOLDOET_NIET
```

Status upgrade: M → D (heuristiek, niet 100% betrouwbaar maar
bruikbaar).

**R13 ("Activiteit helder voor viewer + toepasbare regels") — nu
handmatig:**

Upgrade: als een activiteit een toepasbare regel heeft (R22 = ja), check
dan of de DMN-logica aansluit bij de artikeltekst. Als de checker-score
voor die activiteit <50% is → `voldoet_niet`. Dit maakt R13 meetbaar.

### Databron: OCD als extra collector

odkwaliteit haalt nu alles via DSO-API's (Presenteren v8, RTR v2).
De checker heeft OCD nodig voor DMN-data (i2a-tabellen).

Twee opties:

**A. OCD als aanvullende datasource in de collector:**

```python
# src/odkwaliteit/collector/ocd.py
class OcdCollector:
    """Haalt DMN-data op uit OCD voor categorie G checks."""

    def __init__(self, ocd_api_url: str):
        self.base_url = ocd_api_url

    async def get_dmn_for_activiteit(self, activiteit_id: str) -> dict:
        """Haal DMN-structuur op via OCD API."""
        # GET /v1/activiteit/{id}/toepasbare-regels
        ...

    async def get_artikel_for_activiteit(self, activiteit_id: str) -> str:
        """Haal artikeltekst op via OCD API."""
        # GET /v1/activiteit/{id}/artikeltekst
        ...
```

Vereist: twee nieuwe endpoints in `ocd-api/main.py`.

**B. Categorie G als aparte scoring-pass:**

De standaard odkwaliteit-pipeline draait normaal (collect → score A-F →
export). Daarna draait een aparte pass voor categorie G die rechtstreeks
tegen de OCD-database queried (psycopg, niet via API). Output wordt
samengevoegd in dezelfde `Score`/`Bevinding`-modellen.

```
python -m odkwaliteit collect      # DSO-API's → SQLite
python -m odkwaliteit score        # A-F scoring
python -m odkwaliteit score-g      # G scoring (tegen OCD)
python -m odkwaliteit export       # Alles naar JSON
```

**Aanbeveling: optie B voor nu.** Houdt de standaard odkwaliteit-pipeline
ongewijzigd. Categorie G draait als aparte pass. Later, als OCD-API
stabiel is, kan het naar optie A gemigreerd worden.

### Website-integratie

De odkwaliteit-website toont per document een radardiagram met
categorieën A-F. Met categorie G erbij:

- Radardiagram wordt 7-puntig (A t/m G)
- Detailpagina krijgt een extra sectie "Toepasbare regels" met:
  - Kwaliteitsscore per activiteit
  - Lijst van afwijkingen (zelfde format als stap 5 van de checker)
  - Link naar het regelbestand in het DSO
- Ranglijst krijgt een extra kolom "TR-score" (toepasbare-regel-score)

---

## Implementatieplan

| Fase | Wat | Effort |
|---|---|---|
| 0 | Inventarisatie-query schrijven en valideren op één gemeente | 2 uur |
| 1 | DMN-extractor: `i2a.dmn_element` → gestructureerd JSON | 4 uur |
| 2 | Artikel-extractor: LLM-prompt + parser | 4 uur |
| 3 | Vergelijker: regelgebaseerd + LLM | 4 uur |
| 4 | Rapportgenerator (CLI + JSON + Markdown) | 4 uur |
| 5 | CLI-integratie (`python -m src.cli checker`) | 2 uur |
| 6 | Validatie op 3-5 gemeenten, prompt-tuning | 4 uur |
| 7 | odkwaliteit integratie: `f_toepasbare_regels.py` (R37-R42) | 4 uur |
| 8 | odkwaliteit integratie: R11/R13 upgrade M→D | 4 uur |
| 9 | odkwaliteit integratie: `score-g` CLI-commando + OCD-connector | 4 uur |
| 10 | Website: radardiagram G + detailpagina TR-sectie | 4 uur |
| **Totaal** | | **~5 werkdagen** |
