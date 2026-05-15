# p2pwijziging — aankomende wijzigingen

**Status:** geïmplementeerd
**Datum:** 2026-05-04 (v1: sparse `tekst_delta`)
**Laatste wijziging:** 2026-05-15 (v2: volle `tekst_element`-mirror — zie onder)

---

## Doel

Persisteer de wijzigingen die op de huidige geconsolideerde regelingen
in `p2p` van toepassing worden of zijn — zodat je per regeling kunt
zien wat eraan zit te komen. Bron: DSO Presenteren v8 endpoints
`/ontwerpregelingen` en `/besluitversies`.

Twee soorten in één schema:

| Soort | Wat | API-endpoint |
|---|---|---|
| **Ontwerp** | Nog niet vastgesteld, in voorbereiding/ter inzage | `/ontwerpregelingen/{id}` |
| **Besluitversie** | Vastgesteld besluit, vaak nog niet in werking | `/besluitversies/{id}` |

---

## Schema

Vijf tabellen + twee views, allemaal in `p2pwijziging`:

```
p2pwijziging.besluit
   ontwerpbesluit_id        TEXT PRIMARY KEY
   technisch_id             TEXT NOT NULL UNIQUE
   regeling_work            TEXT NOT NULL          -- /akn/nl/act/... (work-niveau)
   wijzigt_expression       TEXT                    -- huidige expressie die gewijzigd wordt
   nieuwe_expression        TEXT                    -- de versie die het ZAL zijn na vaststelling
   soort                    TEXT NOT NULL CHECK ('ontwerp' | 'besluitversie')
   status                   TEXT NOT NULL CHECK ('ontwerp' | 'ter_inzage' |
                                                  'vastgesteld' | 'in_werking')
   bekend_op                DATE
   ontvangen_op             DATE
   begin_geldigheid         DATE                    -- alleen besluitversie
   begin_inwerking          DATE                    -- alleen besluitversie
   bronhouder               TEXT REFERENCES core.bronhouder
   documenttype             TEXT
   opschrift                TEXT
   ...

p2pwijziging.procedurestap
   ontwerpbesluit_id, soort, voltooid_op, plaats

p2pwijziging.tekst_element        -- volle boom, mirror van p2p.tekst_element
   ontwerpbesluit_id, eid, wid, element_type,
   parent_id (FK self), nummer, opschrift, inhoud,
   inhoud_plain (GENERATED), volgorde,
   wijzigactie ∈ {voegtoe, verwijder, nieuweContainer, verwijderContainer} | NULL,
   vervallen, bevat_renvooi, bevat_ontwerp_informatie

p2pwijziging.annotatie_delta
   ontwerpbesluit_id, type, identificatie,
   bewerking ENUM, naam, payload JSONB

p2pwijziging.locatie_delta
   ontwerpbesluit_id, locatie_id, bewerking,
   locatie_type, noemer, geometrie GEOMETRY(28992)

-- Views
p2pwijziging.ontwerp        = SELECT * FROM besluit WHERE soort = 'ontwerp'
p2pwijziging.besluitversie  = SELECT * FROM besluit WHERE soort = 'besluitversie'
```

### Waarom één tabel met `soort` en geen aparte tabellen

De delta-tabellen (`annotatie_delta`, `locatie_delta`) zijn voor beide
soorten **structureel identiek**. Twee parallelle tabelsets zou alleen
duplicatie geven of polymorfe FK's vereisen (beide lelijker dan een
index op `soort`). De views maken het expliciet.

### Waarom `tekst_element` als volle boom i.p.v. sparse delta (v2, 2026-05-15)

Eerste implementatie was een sparse `tekst_delta` met enum
`bewerking ∈ {toevoegen, wijzigen, verwijderen}`. Bij debug van een
lege tabel bleek:

1. De DSO-API levert de documentstructuur onder soort-specifieke keys
   (`_embedded.ontwerpDocumentComponenten` resp.
   `besluitversieDocumentComponenten`), niet `documentComponenten`.
   De loader zocht naar de verkeerde key — alle 214 wijzigingen kregen
   stilzwijgend 0 rijen.
2. De aangenomen `_delta.bewerking`-key bestaat niet in de payload.
   De echte renvooi zit als JSON-attribuut `wijzigactie` op de
   `DocumentComponent`-nodes met waardenset
   `{voegtoe, verwijder, nieuweContainer, verwijderContainer}` —
   plus `vervallen=true`, `bevatRenvooi`, `bevatOntwerpInformatie`.
3. De API levert sowieso de volle boom (incl. ongewijzigde nodes voor
   context). Sparse opslag betekent voor elke "toon mij wat er wijzigt
   in artikel X met zijn leden ernaast"-query een LEFT JOIN met
   `p2p.tekst_element`.

v2 schrijft daarom de volle boom weg met de echte renvooi-attributen
op de gewijzigde nodes (NULL/FALSE = ongewijzigd), als mirror van
`p2p.tekst_element`. Voordelen:

- Renvooi-namen 1-op-1 zoals STOP ze gebruikt — geen lossy enum.
- Viewer-symmetrie: dezelfde shape als `p2p.tekst_element` (parent_id,
  inhoud_plain GENERATED, FTS-index).
- "Hoe ziet de regeling eruit na vaststelling": filter
  `wijzigactie IS DISTINCT FROM 'verwijder'`.
- Inline renvooi (`<NieuweTekst>`/`<VerwijderdeTekst>` in `Kop`,
  `wijzigactie="voegtoe|verwijder"` op `<Al>` in inhoud) blijft gewoon
  als XML in de `inhoud`/`opschrift`-velden — viewer kan die direct
  stylen, geen aparte tabel.

Migratie: `scripts/2026-05-refactor-p2pwijziging-tekst.sql`. Veilig
om te draaien — `tekst_delta` was leeg.

### Renvooi vs vervangRegeling

Niet elke wijziging is een delta — sommige besluiten vervangen de
hele regeling. Detectie uit de Presenteren-API listing zelf:

| Soort | Renvooi (delta) | VervangRegeling |
|---|---|---|
| Ontwerp | `_links.beoogdeOpvolgerVan` aanwezig | link ontbreekt |
| Besluitversie | `_links.wijzigtRegelingversie` aanwezig | link ontbreekt |

`p2pwijziging.besluit.is_vervang_regeling` houdt dit vast, zodat de
viewer "alles in deze boom is nieuw" weet zonder over alle nodes te
lopen om te checken op `bevat_renvooi`.

### Waarom JSONB voor annotatie-payload

De DSO-API levert annotaties met type-specifieke velden (een
`activiteit` heeft andere velden dan een `gebiedsaanwijzing` of een
`omgevingsnorm`). Volledig genormaliseerd opslaan zou een tiental
parallelle tabellen vereisen. JSONB houdt de structuur intact,
ondersteunt indexen waar nodig, en past bij het delta-karakter (we
slaan de wijziging op, niet de definitie).

---

## Filter-logica

Een wijziging wordt **alleen opgeslagen** als alle drie de
voorwaarden gelden:

```python
def is_relevant(regeling_work, nieuwe_expression, begin_inwerking, bekend_op):
    # 1. Wij kennen deze regeling
    huidige_expression = SELECT frbr_expression
                         FROM p2p.regeling
                         WHERE frbr_work = regeling_work
    if not huidige_expression:
        return False

    # 2. De wijziging introduceert een ANDERE expression
    if nieuwe_expression == huidige_expression:
        return False  # al verwerkt in onze p2p

    # 3a. Voor besluitversies: alleen toekomstige inwerkingtreding
    if begin_inwerking and begin_inwerking < today:
        return False  # al in werking, dus al achterhaald

    # 3b. Voor ontwerpen: bekend_op moet >= datum van onze huidige versie
    if bekend_op and bekend_op < datum(huidige_expression):
        return False  # gebaseerd op verouderde versie

    return True
```

### Hoe dit filter is geëvolueerd

| Iteratie | Filter | Resultaat |
|---|---|---|
| v0 (los) | "wijzigt een regeling die wij hebben" | 2.822 wijzigingen — veel te ruim |
| v1 (datum-snapshot) | "wijziging-datum >= snapshot-datum" | 1.222 — nog veel al-verwerkt |
| v2 (expression-vergelijking) | "nieuwe_expression != onze_expression" + "inwerking ≥ vandaag" | **214** — écht aankomend |

De sleutel-inzicht uit v2: de DSO-API geeft per besluit een
`instrumentversie` (= `expressionId`) die de **nieuwe versie** is na
vaststelling. Als die gelijk is aan onze `p2p.regeling.frbr_expression`,
is het besluit al in onze data verwerkt — niet meer "aankomend".

---

## Refresh-strategie

P2p is een snapshot, niet realtime. Als de DSO een nieuwere
geconsolideerde versie heeft dan wij, klopt het filter niet meer
(we markeren wijzigingen als "aankomend" terwijl ze al verwerkt
zouden moeten zijn in een geüpdatete p2p).

Lichtgewicht refresh via `scripts/refresh_p2p_expressions.py`:
1. Voor elke `p2p.regeling`: vraag de DSO-API om de huidige
   `expressionId` (één API-call per regeling, ~13 minuten voor 1.868 regelingen)
2. Update `p2p.regeling.frbr_expression` waar de DSO een nieuwere
   versie levert dan wij hebben
3. Annotaties / teksten worden NIET opnieuw geladen (te zwaar) —
   die volgen pas bij een volledige re-load

Aanbevolen ritme: wekelijks. Daarna `wijziging ontwerpen` en
`wijziging besluiten` opnieuw draaien om het filter scherp te houden.

---

## Geometrieën

`locatie_delta.geometrie` wordt **default niet automatisch geladen**
tijdens batch-runs. Reden: per locatie een aparte
`GET /geometrieen/{uuid}` API-call, en een ontwerp/besluit kan 100+
locaties hebben → uren per regeling.

De rij wordt wel aangemaakt (met `geometrie = NULL`) zodat de
metadata + bewerking-info bekend is. Backfill van geometrieën via
een aparte loader (gepland: `scripts/fetch_p2pwijziging_geometries.py`).

---

## CLI

```bash
python -m src.cli wijziging ontwerpen   # alle relevante ontwerpen laden
python -m src.cli wijziging besluiten   # alle relevante besluitversies laden
python -m src.cli wijziging status      # overzicht per soort + status
```

---

## Voorbeeld-queries

### Wat staat er aan te komen op een regeling?

```sql
SELECT b.soort, b.opschrift, b.bekend_op, b.begin_inwerking,
       b.status, b.is_vervang_regeling,
       count(te.id) FILTER (WHERE te.wijzigactie IS NOT NULL OR te.vervallen) AS tekst_wijzigingen,
       count(ad.id) AS annotatie_wijzigingen
FROM p2pwijziging.besluit b
LEFT JOIN p2pwijziging.tekst_element te ON te.ontwerpbesluit_id = b.ontwerpbesluit_id
LEFT JOIN p2pwijziging.annotatie_delta ad ON ad.ontwerpbesluit_id = b.ontwerpbesluit_id
WHERE b.regeling_work = '/akn/nl/act/gm0344/2020/omgevingsplan'
GROUP BY b.ontwerpbesluit_id, b.soort, b.opschrift, b.bekend_op,
         b.begin_inwerking, b.status, b.is_vervang_regeling
ORDER BY b.begin_inwerking NULLS LAST;
```

### Welke artikelen krijgen wijzigingen?

```sql
SELECT b.opschrift AS besluit,
       te.wijzigactie, te.vervallen, te.nummer, te.opschrift AS artikel
FROM p2pwijziging.besluit b
JOIN p2pwijziging.tekst_element te ON te.ontwerpbesluit_id = b.ontwerpbesluit_id
WHERE b.regeling_work = '/akn/nl/act/gm0344/2020/omgevingsplan'
  AND te.element_type = 'Artikel'
  AND (te.wijzigactie IS NOT NULL OR te.vervallen)
ORDER BY te.wijzigactie NULLS LAST, te.nummer;
```

### Hoe ziet de regeling eruit ná vaststelling?

```sql
-- "Toon de regeling-na-besluit": laat verwijderde nodes weg, behoud
-- de rest van de boom in volgorde.
SELECT te.element_type, te.nummer, te.opschrift, te.inhoud_plain
FROM p2pwijziging.tekst_element te
JOIN p2pwijziging.besluit b ON b.ontwerpbesluit_id = te.ontwerpbesluit_id
WHERE b.ontwerpbesluit_id = :besluit_id
  AND te.wijzigactie IS DISTINCT FROM 'verwijder'
  AND NOT te.vervallen
ORDER BY te.volgorde;
```

### Welke nieuwe activiteiten worden voorgesteld?

```sql
SELECT b.opschrift AS besluit, ad.naam AS activiteit, ad.bewerking,
       ad.payload->>'kwalificatie' AS kwalificatie
FROM p2pwijziging.besluit b
JOIN p2pwijziging.annotatie_delta ad ON ad.ontwerpbesluit_id = b.ontwerpbesluit_id
WHERE ad.type = 'activiteit'
  AND b.regeling_work LIKE '%gm0344%'
ORDER BY ad.bewerking, ad.naam;
```

---

## Beperkingen en open vragen

- **Geometrieën:** moeten via aparte backfill-loader. Niet automatisch.
- **Versionering bij vaststelling:** als een ontwerp wordt vastgesteld,
  ontstaat er een besluitversie met dezelfde inhoud. We persisteren
  beide (ontwerp blijft staan tot expliciet verwijderd). Idee: een
  cleanup-job die ontwerpen verwijdert nadat het bijbehorende besluit
  in werking is getreden.
- **Multiple ontwerpen per regeling:** kan voorkomen (verschillende
  wijzigingsbesluiten in voorbereiding). Geen technisch probleem,
  maar de viewer moet dit ondersteunen.
- **DSO-snapshot drift:** zonder regelmatige refresh van `p2p.regeling`
  loopt het filter uit de pas. Cron-job aanbevolen.

---

## Volume (snapshot 2026-05-04)

| Soort | Aantal | Annotatie-deltas | Locatie-deltas |
|---|---|---|---|
| Ontwerp | 198 | ~280K | ~900K |
| Besluitversie | 16 | ~80K | ~180K |
| **Totaal** | **214** | **363.231** | **1.083.472** |

T.o.v. ruwe totalen op DSO (835 ontwerpen + ~6000 besluitversies)
filtert het filter ~95% weg — alleen wijzigingen die op onze huidige
geconsolideerde versie van toepassing zijn blijven over.
