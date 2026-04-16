# Bestemmingsplan → Omgevingsplan Converter — Ontwerp

**Status:** ontwerp
**Datum:** 2026-04-15
**Relatie:** ideeën #3 en #4 uit vault_v1/ideeen.md

---

## Doel

Een conversie-assistent die bestemmingsplannen (Wro/IMRO) omzet naar
een omgevingsplan-formaat (Ow/STOP/IMOW). Niet als "af" product, maar
als een zo ver mogelijk geautomatiseerd startpunt dat een planner
weken handwerk bespaart.

Drie stappen met afnemende automatiseringsgraad:

| Stap | Automatisering | Levert op |
|---|---|---|
| 1. Structuurconversie | 100% mechanisch | Publiceerbaar STOP-document + GIO's + basis gebiedsaanwijzingen |
| 2. Annotatievoorstel | ~80% LLM-ondersteund | Voorgestelde activiteiten, normen, juridische regels, thema's |
| 3. Plannerreview | 0% automatisch | Gevalideerd omgevingsplan na menselijke beoordeling |

---

## Output-schema: `conv`

De conversie-output komt in een apart schema `conv` — niet in `p2p`.
Redenen:
- **Geen vervuiling:** autoritatieve DSO-data (p2p) en afgeleide
  conversie-voorstellen mengen niet
- **Herhaalbaar:** `TRUNCATE conv.*` of droppen per gemeente zonder
  p2p te raken
- **Zelfde tabelstructuur als p2p:** dezelfde queries werken met
  `conv.` prefix (inclusief `wat_geldt_hier`, `zoek_tekst`)

De tabellen in `conv` zijn een subset van p2p, plus een eigen
metadatatabel:

```
conv.conversie_meta              — bron-instrument, stap, bron-type, timestamp, llm-model
conv.regeling                    — 1 per geconverteerd bestemmingsplan
conv.tekst_element               — geconverteerde artikeltekst (boom via parent_id)
conv.locatie                     — planobject-geometrieën als Ow-locaties
conv.locatiegroep_lid            — groepering van locaties
conv.gebiedsaanwijzing           — afgeleid uit bestemmingshoofdgroep (stap 1)
conv.activiteit                  — LLM-voorstellen (stap 2)
conv.juridische_regel            — afgeleid (stap 2)
conv.activiteit_locatieaanduiding — afgeleid (stap 2)
conv.juridische_regel_gebiedsaanwijzing — afgeleid (stap 2)
conv.norm                        — LLM-geëxtraheerd (stap 2)
conv.normwaarde                  — LLM-geëxtraheerd (stap 2)
conv.juridische_regel_norm       — afgeleid (stap 2)
```

---

## Databronnen (alles in OCD)

**wro-schema (input — immutable brondata):**
```
wro.ruimtelijk_instrument     — het bestemmingsplan zelf (naam, plangebied-geometrie, planstatus)
wro.planobject                — enkelbestemmingen, dubbelbestemmingen, bouwvlakken, etc.
                                met PostGIS-geometrie en bestemmingshoofdgroep
wro.wro_tekst_object          — de artikeltekst in hiërarchie (parent_id, niveau 0-11)
wro.wro_geleideformulier      — IMRO-versie metadata
wro.wro_bronbestand           — bijbehorende bestanden
```

**p2p-schema (referentie, voor bruidsschat-interactie — niet gewijzigd):**
```
p2p.regeling                  — het huidige omgevingsplan van de gemeente
p2p.tekst_element             — de huidige omgevingsplantekst (voor conflictdetectie)
p2p.activiteit                — bestaande activiteiten (voor naamgeving-consistentie)
p2p.gebiedsaanwijzing         — bestaande gebiedsaanwijzingen
p2p.norm / p2p.normwaarde     — bestaande normen
```

**core-schema:**
```
core.bronhouder               — gemeentenaam, overheidscode
core.bestemmingshoofdgroep    — waardelijst voor mapping
```

**conv-schema (output — afgeleid, herhaalbaar):**
```
conv.*                        — zie "Output-schema" hierboven
```

---

## Stap 1: Structuurconversie (100% mechanisch)

Output gaat naar het `conv`-schema. Puur SQL-transformaties van
`wro.*` → `conv.*`. Geen XML-generatie, geen bestanden — alles
blijft in de database. STOP XML export is een optionele latere stap.

### 1.1 Regeling aanmaken

```sql
INSERT INTO conv.regeling (
    frbr_expression, frbr_work, regelingmodel, opschrift,
    bronhouder, documenttype
)
SELECT
    '/akn/nl/act/gm' || ri.bronhouder || '/conv/' || ri.idn || '/nld@1',
    '/akn/nl/act/gm' || ri.bronhouder || '/conv/' || ri.idn,
    'RegelingKlassiek',
    'Omgevingsplan ' || b.naam || ', deel ' || ri.naam,
    ri.bronhouder,
    'Omgevingsplan'
FROM wro.ruimtelijk_instrument ri
JOIN core.bronhouder b ON b.overheidscode = ri.bronhouder
WHERE ri.idn = %s;
```

### 1.2 Tekst overnemen

**Input:** `wro.wro_tekst_object` met hiërarchie via `parent_id`.

**Mapping naar `conv.tekst_element`:**

| wro.wro_tekst_object | conv.tekst_element |
|---|---|
| object_type "hoofdstuk" | element_type "Hoofdstuk" |
| object_type "paragraaf" | element_type "Afdeling" |
| object_type "artikel" | element_type "Artikel" |
| object_type "lid" | element_type "Lid" |
| naam / label | opschrift |
| nummer | nummer |
| inhoud (HTML) | inhoud (ongewijzigd overgenomen) |
| parent_id → volgt hiërarchie | parent_id → zelfde hiërarchie |

**eId-generatie:** bestemmingsplannen hebben geen eId's. Die worden
gegenereerd volgens STOP-conventie:

```
Hoofdstuk 1 "Inleidende regels"     → chp_1
  Artikel 1 "Begrippen"             → chp_1__art_1
    Lid 1                           → chp_1__art_1__lid_1
Hoofdstuk 2 "Bestemmingsregels"     → chp_2
  Artikel 3 "Wonen"                 → chp_2__art_3
```

```sql
INSERT INTO conv.tekst_element (
    regeling_expression, eid, wid, element_type,
    parent_id, nummer, opschrift, inhoud, volgorde
)
SELECT
    %s,  -- regeling_expression uit stap 1.1
    -- eId: gegenereerd op basis van positie in boom
    generate_eid(wto.object_type, wto.volgnummer, wto.niveau),
    -- wId: uniek per element
    'gm' || %s || '__' || generate_eid(wto.object_type, wto.volgnummer, wto.niveau),
    CASE wto.object_type
        WHEN 'hoofdstuk' THEN 'Hoofdstuk'
        WHEN 'paragraaf' THEN 'Afdeling'
        WHEN 'artikel' THEN 'Artikel'
        WHEN 'lid' THEN 'Lid'
        ELSE 'Divisietekst'
    END,
    NULL,  -- parent_id: wordt in tweede pass gezet
    wto.nummer,
    COALESCE(wto.naam, wto.label),
    wto.inhoud,
    wto.volgnummer
FROM wro.wro_tekst_object wto
WHERE wto.instrument_idn = %s
ORDER BY wto.volgnummer;
```

Parent-id's worden in een tweede pass gezet (zelfde patroon als de
bestaande loaders). eId-generatie is een Python-functie die de
STOP-conventie volgt op basis van nesting-niveau en volgnummer.

**Bekende edge cases:**
- Sommige bestemmingsplannen hebben geen nette hiërarchie (alles op
  niveau 0). Fallback: genereer één Hoofdstuk met alle artikelen.
- HTML kan niet-valide zijn. Wordt ongewijzigd overgenomen (de
  bestaande p2p.tekst_element.inhoud is ook HTML).

### 1.3 Locaties aanmaken uit planobjecten

**Per planobject → één `conv.locatie`:**

```sql
INSERT INTO conv.locatie (identificatie, locatie_type, noemer, geometrie)
SELECT
    'nl.imow-gm' || ri.bronhouder || '.locatie.conv-' || md5(po.identificatie),
    'Gebied',
    COALESCE(po.naam, po.bestemmingshoofdgroep, po.object_type),
    po.geometrie  -- zelfde CRS, geen conversie nodig
FROM wro.planobject po
JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
WHERE po.instrument_idn = %s;
```

**Groepering:** planobjecten van hetzelfde type met dezelfde
bestemmingshoofdgroep worden gegroepeerd in een `conv.locatiegroep_lid`.
Bijvoorbeeld: alle enkelbestemmingen "Wonen" → één locatiegroep.

```sql
-- Locatiegroep per bestemmingshoofdgroep
INSERT INTO conv.locatie (identificatie, locatie_type, noemer, geometrie)
SELECT
    'nl.imow-gm' || ri.bronhouder || '.locatiegroep.conv-' || md5(po.bestemmingshoofdgroep),
    'Gebiedengroep',
    po.bestemmingshoofdgroep,
    ST_Union(po.geometrie)  -- union van alle planobjecten in deze groep
FROM wro.planobject po
JOIN wro.ruimtelijk_instrument ri ON ri.idn = po.instrument_idn
WHERE po.instrument_idn = %s
  AND po.bestemmingshoofdgroep IS NOT NULL
GROUP BY ri.bronhouder, po.bestemmingshoofdgroep;
```

### 1.4 Gebiedsaanwijzingen afleiden

**Mappingtabel bestemmingshoofdgroep → Ow functie-aanduiding:**

| bestemmingshoofdgroep | gebiedsaanwijzing.type | gebiedsaanwijzing.groep |
|---|---|---|
| Agrarisch | Functie | Agrarische functie |
| Agrarisch met waarden | Functie | Agrarische functie |
| Bedrijf | Functie | Bedrijfsfunctie |
| Bedrijventerrein | Functie | Bedrijfsfunctie |
| Bos | Functie | Groenvoorziening |
| Centrum | Functie | Gemengde dorps- en stadsgebiedfunctie |
| Cultuur en ontspanning | Functie | Maatschappelijke functie |
| Detailhandel | Functie | Detailhandelfunctie |
| Dienstverlening | Functie | Maatschappelijke functie |
| Gemengd | Functie | Gemengde dorps- en stadsgebiedfunctie |
| Groen | Functie | Groenvoorziening |
| Horeca | Functie | Horecafunctie |
| Kantoor | Functie | Kantoorfunctie |
| Maatschappelijk | Functie | Maatschappelijke functie |
| Natuur | Functie | Groenvoorziening |
| Recreatie | Functie | Recreatiefunctie |
| Sport | Functie | Sportfunctie |
| Tuin | Functie | Woonfunctie |
| Verkeer | Functie | Verkeersfunctie |
| Water | Functie | Waterfunctie |
| Wonen | Functie | Woonfunctie |
| Woongebied | Functie | Woonfunctie |
| Overig | Functie | Overige functie |

**Dubbelbestemmingen:**

| dubbelbestemmingshoofdgroep | gebiedsaanwijzing.type | gebiedsaanwijzing.groep |
|---|---|---|
| Leiding | Beperkingengebied | Leidingstrook |
| Waarde | Functie | Waardevol gebied |
| Waterstaat | Beperkingengebied | Waterstaatswerk |

**Per gebiedsaanwijzing:**
- `identificatie`: gegenereerd (`nl.imow-gm{cbs}.gebiedsaanwijzing.{uuid}`)
- `naam`: planobject.naam of bestemmingshoofdgroep
- `locatie_id`: verwijzing naar de GIO-locatie uit stap 1.2

### 1.5 Regelingsgebied

```sql
INSERT INTO conv.locatie (identificatie, locatie_type, noemer, geometrie)
SELECT
    'nl.imow-gm' || ri.bronhouder || '.locatie.conv-regelingsgebied-' || md5(ri.idn),
    'Gebied',
    'Regelingsgebied ' || ri.naam,
    ri.geometrie
FROM wro.ruimtelijk_instrument ri
WHERE ri.idn = %s;
```

Het plangebied van het bestemmingsplan wordt het regelingsgebied van
het apart-deel.

### 1.6 Conversie-metadata

```sql
INSERT INTO conv.conversie_meta (
    instrument_idn, regeling_expression, stap, bron,
    geconverteerd_op
)
VALUES (%s, %s, 1, 'mechanisch', NOW());
```

### Wat stap 1 oplevert

| conv-tabel | Gevuld | Bron |
|---|---|---|
| `conv.regeling` | 1 rij | mechanisch |
| `conv.tekst_element` | ~30-200 rijen | wro.wro_tekst_object |
| `conv.locatie` | ~50-500 rijen | wro.planobject geometrie |
| `conv.locatiegroep_lid` | ~10-30 rijen | groepering op bestemmingshoofdgroep |
| `conv.gebiedsaanwijzing` | ~10-30 rijen | mappingtabel |
| `conv.conversie_meta` | 1 rij | metadata |
| **conv.activiteit** | **leeg** | **stap 2** |
| **conv.juridische_regel** | **leeg** | **stap 2** |
| **conv.norm / normwaarde** | **leeg** | **stap 2** |

**Direct bruikbaar:**
- `wat_geldt_hier`-query werkt op `conv.*` (zelfde tabelstructuur als p2p)
- `zoek_tekst` op `conv.tekst_element` werkt (zelfde kolommen)
- Omgevingsbot kan conv-data doorzoeken als extra bron
- Gebiedsaanwijzingen zijn ruimtelijk querybaar via PostGIS

**Nog niet bruikbaar:**
- Geen activiteiten → geen vergunningcheck
- Geen juridische regels → geen koppeling tekst↔kaart
- Geen normen → geen gestructureerde waarden

---

## Stap 2: Annotatievoorstel (~80% LLM-ondersteund)

### 2.1 Activiteiten voorstellen

**Per artikel in de geconverteerde tekst:**

LLM-prompt:
```
Analyseer onderstaand artikel uit een bestemmingsplan dat wordt
geconverteerd naar een omgevingsplan. Stel de IMOW-activiteiten voor
die uit dit artikel volgen.

Per activiteit geef:
- naam (in Ow-stijl: zelfstandig naamwoord + werkwoord, lowercase)
- kwalificatie: vergunningplichtig | meldingsplichtig | verbod |
  toegestaan | anders_geduid
- bovenliggende activiteit (als er een hiërarchie is)

ARTIKEL:
---
{artikeltekst}
---

BESTEMMING: {bestemmingshoofdgroep}
BESTAANDE ACTIVITEITEN IN HET OMGEVINGSPLAN VAN DEZE GEMEENTE:
{bestaande_activiteiten_namen}
```

**Context meegeven:** de bestaande activiteiten in het omgevingsplan van
die gemeente (uit `p2p.activiteit`) zodat de LLM consistente naamgeving
gebruikt.

**Output markeren:** elke voorgestelde activiteit krijgt `bron: "llm-voorstel"`
zodat de planner weet wat gereviewed moet worden.

### 2.2 Normen extraheren

**Per artikel dat kwantitatieve eisen bevat:**

LLM-prompt:
```
Extraheer alle kwantitatieve normen uit dit bestemmingsplanartikel.

Per norm geef:
- naam (bv. "maximale bouwhoogte", "maximaal bebouwingspercentage")
- type_norm (bv. "maximum", "minimum", "exact")
- eenheid (bv. "m", "m²", "%", "stuks")
- waarden: lijst van {waarde, locatie-omschrijving}

ARTIKEL:
---
{artikeltekst}
---

MAATVOERING UIT PLANOBJECTEN (indien beschikbaar):
{maatvoering_info_json}
```

**Verrijking met maatvoering:** `wro.planobject.maatvoering_info` (JSONB)
bevat soms gestructureerde waarden (bouwhoogte, goothoogte, etc.). Deze
meegeven als extra context verbetert de extractie.

### 2.3 Juridische regels genereren

Per voorgestelde activiteit + artikel → een `juridische_regel` met:
- `regel_type`: uit de kwalificatie van de activiteit
- `regeltekst_wid`: verwijzing naar het STOP-artikel (uit stap 1)
- `thema`: afgeleid uit bestemmingshoofdgroep (Wonen → "wonen",
  Bedrijf → "economie", etc.)

Koppeltabellen:
- `activiteit_locatieaanduiding`: activiteit + locatie (GIO uit stap 1) + kwalificatie
- `juridische_regel_gebiedsaanwijzing`: regel + gebiedsaanwijzing (uit stap 1)
- `juridische_regel_norm`: regel + norm (uit stap 2.2)

### 2.4 Bruidsschat-conflictdetectie

```sql
-- Zoek bestaande omgevingsplan-artikelen die over dezelfde
-- onderwerpen gaan als het bestemmingsplan
SELECT te.opschrift, te.inhoud, jr.thema
FROM p2p.tekst_element te
JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
WHERE r.bronhouder = %s
  AND r.documenttype = 'Omgevingsplan'
  AND jr.thema && %s  -- overlap in thema-arrays
```

Per gevonden overlap: LLM-vergelijking van de bestemmingsplanartikel
vs. de omgevingsplanartikel. Output: "conflict", "complementair", of
"duplicaat".

### 2.5 Tophaak-activiteit

Mechanisch aanmaken (naamconventie is vastgelegd):

```
"Activiteit gereguleerd in het omgevingsplan van gemeente {naam},
deel {bestemmingsplannaam}"
```

Alle voorgestelde activiteiten krijgen deze tophaak als
`bovenliggendeActiviteitRef`.

### Wat stap 2 oplevert (bovenop stap 1)

| Component | Status | Betrouwbaarheid |
|---|---|---|
| Activiteiten (voorgesteld) | Ja, LLM-gebaseerd | ~80%, review nodig |
| Normen + normwaarden | Ja, LLM + maatvoering | ~85% als maatvoering beschikbaar |
| Juridische regels | Ja, afgeleid | Afhankelijk van activiteit-kwaliteit |
| Thema's | Ja, afgeleid uit bestemming | ~90% |
| Bruidsschat-conflicten | Ja, gesignaleerd | ~75%, LLM-vergelijking |
| Tophaak | Ja, mechanisch | 100% |

**Alle LLM-output wordt gemarkeerd als `bron: "llm-voorstel"`.** Niets
wordt als definitief gepresenteerd.

---

## Stap 3: Plannerreview (0% automatisch)

### 3.1 Review-interface

Het resultaat van stap 1+2 wordt gepresenteerd als een reviewbaar
pakket:

```
output/{gemeente}_{bestemmingsplan}/
├── stop/                    # Publiceerbare STOP-bestanden (stap 1)
├── ow/                      # OW-objecten incl. voorstellen (stap 2)
├── review/
│   ├── samenvatting.md      # Overzicht: wat is geconverteerd, wat is voorgesteld
│   ├── activiteiten.csv     # Alle voorgestelde activiteiten met bron-markering
│   ├── normen.csv           # Alle geëxtraheerde normen
│   ├── conflicten.md        # Bruidsschat-conflicten met toelichting
│   └── onzekerheden.md      # LLM-onzekerheden en edge cases
└── validatie/
    ├── stop-validatie.log   # STOP-schema validatie
    └── ow-validatie.log     # IMOW-schema validatie
```

### 3.2 Wat de planner beoordeelt

| Onderdeel | Actie planner |
|---|---|
| Activiteit-namen | Goedkeuren, hernoemen, of verwijderen |
| Kwalificaties | Vergunningplichtig ↔ meldingsplichtig ↔ verbod |
| Norm-waarden | Controleren tegen bestemmingsplan-kaart |
| Bruidsschat-conflicten | Beslissen: bestemmingsplan of bruidsschat prevaleert |
| Hiërarchie | Activiteiten groeperen/herordenen |
| Thema-toekenning | Controleren en aanvullen |

### 3.3 Feedback-loop

De planner-correcties kunnen teruggevoerd worden als training-data voor
de LLM-prompts: "voor gemeente X heb je activiteit Y voorgesteld, maar
de planner heeft het gewijzigd naar Z". Over tijd verbetert de
nauwkeurigheid van stap 2.

---

## Architectuur

```
┌──────────────────────────────────────────────────────────────┐
│                 Converter CLI                                │
│  python -m src.cli convert --gemeente 0344 --plan NL.IMRO... │
│  python -m src.cli convert --gemeente 0344 --all             │
└─────────────┬────────────────────────────────────────────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 1a: Regeling   │  wro.ruimtelijk_instrument → conv.regeling
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 1b: Tekst      │  wro.wro_tekst_object → conv.tekst_element
   │ + eId-generatie      │  hiërarchie behouden, eId's genereren
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 1c: Locaties   │  wro.planobject → conv.locatie
   │ + groepering         │  geometrie overnemen, groeperen per bestemming
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 1d: Gebiedsaan-│  bestemmingshoofdgroep → conv.gebiedsaanwijzing
   │ wijzingen            │  vaste mappingtabel
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 2a: Activiteit-│  LLM per artikel → conv.activiteit
   │ voorstel             │  context: bestaande p2p.activiteiten
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 2b: Normen     │  LLM + maatvoering_info → conv.norm + normwaarde
   │ extractie            │
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 2c: Juridische │  Afleiden → conv.juridische_regel + junctions
   │ regels + thema's     │
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Stap 2d: Bruidsschat│  conv.tekst_element vs. p2p.tekst_element
   │ conflictdetectie     │  → conv.conversie_meta (conflicten gelogd)
   └──────────┬──────────┘
              │
   ┌──────────▼──────────┐
   │ Output (optioneel): │  conv.* → STOP XML + GIO export
   │ STOP-export          │  alleen nodig voor publicatie naar LVBB
   └──────────┴──────────┘
```

---

## Kostenschatting

### Stap 1 (mechanisch)

Geen LLM-kosten. Puur SQL-transformaties.

Per bestemmingsplan: <1 seconde (INSERT ... SELECT).
Per gemeente (gemiddeld ~15 vigerende plannen): <15 seconden.
Landelijk (~55.000 plannen): ~15 minuten.

### Stap 2 (LLM-ondersteund)

Per bestemmingsplan: gemiddeld ~30 artikelen × 1 LLM-call = ~30 calls.
Per call: ~2000 tokens input + ~500 tokens output = ~2500 tokens.
Per bestemmingsplan: ~75K tokens ≈ ~$0.25 (Claude Sonnet).
Per gemeente (~15 plannen): ~$3.75.
Landelijk (~55.000 plannen): ~$14.000.

**NB:** landelijke run is niet de use case. De converter draait per
gemeente, per bestemmingsplan, on-demand.

---

## Beperkingen

| Beperking | Impact | Mitigatie |
|---|---|---|
| HTML in bestemmingsplannen is vaak niet-valide | STOP-conversie faalt op edge cases | HTML-sanitizer (BeautifulSoup) als preprocessing |
| Niet alle bestemmingsplannen hebben nette hiërarchie | eId-generatie onbetrouwbaar | Fallback: platte artikellijst zonder hiërarchie |
| Maatvoering_info is niet universeel gevuld | Normen-extractie leunt zwaarder op LLM | Acceptabel: LLM is ~80% betrouwbaar |
| Bestemmingsplan-systematiek verschilt per gemeente | Geen universele mapping mogelijk | LLM-voorstel past zich aan per tekst |
| Bruidsschat-interactie is complex | False positives bij conflictdetectie | Markeer als "te beoordelen", niet als "conflict" |
| STOP-schema evolueert | Gegenereerde XML kan verouderen | Template-versie configureerbaar (STOP 1.3 / 1.4) |

---

## Synergie met andere tools

**Toepasbare-regel-checker (idee #2):**
Na conversie kan de checker de kwaliteit van de voorgestelde annotaties
beoordelen: "zijn de LLM-voorgestelde activiteiten consistent met de
artikeltekst?" Dit geeft de planner een objectieve kwaliteitsscore
op het conversie-resultaat.

**odkwaliteit (annotatieconformiteit):**
Het geconverteerde document kan door odkwaliteit gescoord worden op de
36 richtlijnen. Score na stap 1: laag (geen annotaties). Score na stap 2:
middel-hoog (voorgestelde annotaties). Score na stap 3: hoog (gevalideerd).
Dit maakt de voortgang van de conversie meetbaar.

**Omgevingsbot (idee #5):**
Cross-regime vragen ("mag ik hier een dakkapel?") worden beter
beantwoord als het bestemmingsplan in Ow-formaat is geïndexeerd: de
bot kan dan bestemmingsplanregels net zo doorzoeken als omgevingsplanregels.

---

## DDL voor conv-schema

```sql
CREATE SCHEMA IF NOT EXISTS conv;
COMMENT ON SCHEMA conv IS 'Conversie-output: bestemmingsplan → omgevingsplan (afgeleid, herhaalbaar)';

-- Metadata per conversie-run
CREATE TABLE IF NOT EXISTS conv.conversie_meta (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_idn      TEXT NOT NULL,       -- bron: wro.ruimtelijk_instrument.idn
    regeling_expression TEXT NOT NULL,        -- doel: conv.regeling.frbr_expression
    stap                INT NOT NULL,         -- 1 = mechanisch, 2 = LLM-ondersteund
    bron                TEXT NOT NULL,         -- 'mechanisch' | 'llm-voorstel'
    geconverteerd_op    TIMESTAMP NOT NULL DEFAULT NOW(),
    llm_model           TEXT NULL,            -- bv. 'claude-sonnet-4-6' (alleen stap 2)
    notities            TEXT NULL
);

-- Zelfde structuur als p2p-equivalenten
CREATE TABLE IF NOT EXISTS conv.regeling (
    frbr_expression     TEXT PRIMARY KEY,
    frbr_work           TEXT NOT NULL,
    regelingmodel       TEXT NOT NULL,
    opschrift           TEXT NOT NULL,
    bronhouder          TEXT NULL REFERENCES core.bronhouder(overheidscode),
    documenttype        TEXT NULL
);

CREATE TABLE IF NOT EXISTS conv.tekst_element (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    regeling_expression TEXT NOT NULL REFERENCES conv.regeling(frbr_expression) ON DELETE CASCADE,
    eid                 TEXT NOT NULL,
    wid                 TEXT NOT NULL,
    element_type        TEXT NOT NULL,
    parent_id           BIGINT NULL REFERENCES conv.tekst_element(id) ON DELETE CASCADE,
    nummer              TEXT NULL,
    opschrift           TEXT NULL,
    inhoud              TEXT NULL,
    volgorde            INT NOT NULL DEFAULT 0,
    UNIQUE (regeling_expression, eid)
);

CREATE TABLE IF NOT EXISTS conv.locatie (
    identificatie       TEXT PRIMARY KEY,
    locatie_type        TEXT NOT NULL,
    noemer              TEXT NULL,
    geometrie           GEOMETRY(Geometry, 28992) NOT NULL,
    bron_planobject     TEXT NULL   -- referentie naar wro.planobject.identificatie
);
CREATE INDEX IF NOT EXISTS idx_conv_locatie_geom ON conv.locatie USING GIST(geometrie);

CREATE TABLE IF NOT EXISTS conv.locatiegroep_lid (
    groep_identificatie TEXT NOT NULL REFERENCES conv.locatie(identificatie) ON DELETE CASCADE,
    lid_identificatie   TEXT NOT NULL REFERENCES conv.locatie(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (groep_identificatie, lid_identificatie)
);

CREATE TABLE IF NOT EXISTS conv.gebiedsaanwijzing (
    identificatie       TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    bron                TEXT NOT NULL DEFAULT 'mechanisch'  -- 'mechanisch' | 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.activiteit (
    identificatie       TEXT PRIMARY KEY,
    naam                TEXT NOT NULL,
    groep               TEXT NULL,
    bovenliggende       TEXT NULL REFERENCES conv.activiteit(identificatie),
    is_tophaak          BOOLEAN NOT NULL DEFAULT FALSE,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel (
    identificatie       TEXT PRIMARY KEY,
    regel_type          TEXT NOT NULL,
    thema               TEXT[] NULL,
    regeltekst_wid      TEXT NOT NULL,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.activiteit_locatieaanduiding (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    juridische_regel_id TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    activiteit_id       TEXT NOT NULL REFERENCES conv.activiteit(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    kwalificatie        TEXT NULL
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel_gebiedsaanwijzing (
    juridische_regel_id  TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    gebiedsaanwijzing_id TEXT NOT NULL REFERENCES conv.gebiedsaanwijzing(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, gebiedsaanwijzing_id)
);

CREATE TABLE IF NOT EXISTS conv.norm (
    identificatie       TEXT PRIMARY KEY,
    norm_type           TEXT NOT NULL,
    naam                TEXT NOT NULL,
    type_norm           TEXT NULL,
    eenheid             TEXT NULL,
    bron                TEXT NOT NULL DEFAULT 'llm-voorstel'
);

CREATE TABLE IF NOT EXISTS conv.normwaarde (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    norm_id             TEXT NOT NULL REFERENCES conv.norm(identificatie) ON DELETE CASCADE,
    locatie_id          TEXT NOT NULL REFERENCES conv.locatie(identificatie),
    kwalitatieve_waarde TEXT NULL,
    kwantitatieve_waarde NUMERIC NULL
);

CREATE TABLE IF NOT EXISTS conv.juridische_regel_norm (
    juridische_regel_id TEXT NOT NULL REFERENCES conv.juridische_regel(identificatie) ON DELETE CASCADE,
    norm_id             TEXT NOT NULL REFERENCES conv.norm(identificatie) ON DELETE CASCADE,
    PRIMARY KEY (juridische_regel_id, norm_id)
);
```

---

## Implementatieplan

| Fase | Wat | Effort |
|---|---|---|
| **Stap 1** | | |
| 1.0 | DDL: `conv`-schema aanmaken (bovenstaande SQL) | 1 uur |
| 1.1 | Regeling-aanmaak (wro.ruimtelijk_instrument → conv.regeling) | 1 uur |
| 1.2 | Tekst-conversie (wro.wro_tekst_object → conv.tekst_element + eId-generatie) | 4 uur |
| 1.3 | Locatie-aanmaak (wro.planobject → conv.locatie + locatiegroep_lid) | 3 uur |
| 1.4 | Gebiedsaanwijzing-mapper (bestemmingshoofdgroep → conv.gebiedsaanwijzing) | 2 uur |
| 1.5 | Conversie-meta logging | 1 uur |
| 1.6 | CLI-integratie (`python -m src.cli convert`) + test op 3 plannen | 4 uur |
| | **Subtotaal stap 1:** | **~2 werkdagen** |
| **Stap 2** | | |
| 2.0 | Activiteit-voorstel LLM-prompt + parser → conv.activiteit | 4 uur |
| 2.1 | Normen-extractor (LLM + maatvoering_info) → conv.norm + normwaarde | 4 uur |
| 2.2 | Juridische-regel generator → conv.juridische_regel + junctions | 3 uur |
| 2.3 | Bruidsschat-conflictdetectie (conv vs. p2p) | 4 uur |
| 2.4 | Conversie-meta + review-rapport (Markdown) | 3 uur |
| 2.5 | Validatie op 3-5 gemeenten, prompt-tuning | 6 uur |
| | **Subtotaal stap 2:** | **~3 werkdagen** |
| **Stap 3** | | |
| 3.0 | Review-interface ontwerp (CLI of web) | Scope TBD |
| 3.1 | Feedback-loop voor prompt-verbetering | Scope TBD |
| | **Subtotaal stap 3:** | **TBD** |
| **Optioneel** | | |
| X.1 | STOP XML export vanuit conv-tabellen (voor LVBB-publicatie) | 4 werkdagen |
| | | |
| **Totaal stap 1+2** | | **~5 werkdagen** |
