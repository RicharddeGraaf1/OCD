# Citeertitel uit de Presenteren-API overnemen

> Status: Open
> Datum: 2026-08-07

> **Let op — dit gaat over de REGELING.** Er is een tweede, inmiddels opgelost
> geval één niveau lager: de citeertitel van het **besluit**, uit
> `besluitMetadata.citeerTitel`. Zie [Citeertitel van het besluit
> (`p2pwijziging.besluit`)](#citeertitel-van-het-besluit-p2pwijzigingbesluit)
> onderaan. De twee kolommen heten hetzelfde maar dragen iets anders; verwar ze
> niet.

## Probleem

`p2p.regeling.citeertitel` is voor **alle 1977 regelingen identiek aan
`opschrift`**:

```sql
SELECT count(*) FILTER (WHERE citeertitel IS NOT NULL AND citeertitel <> opschrift)
FROM p2p.regeling WHERE NOT inactief;   -- 0
```

De kolom is dus onbruikbaar. Afnemers die een korte naam willen tonen, moeten
hem zelf uit de titel peuteren.

De oorzaak zit in de loader. `find_regelingen` neemt alleen `officieleTitel`
over ([api_loader.py:96](../dso-loader/src/loaders/api_loader.py)), en de insert
schrijft diezelfde waarde in béide kolommen
([api_loader.py:1061-1066](../dso-loader/src/loaders/api_loader.py)):

```python
(expression_id, regeling_uri, regelingmodel,
 reg.get("titel", ""), reg.get("titel", ""),   # opschrift én citeertitel
 bronhouder_code, doc_type),
```

**De bron levert het wél.** Het regeling-object uit
`GET /regelingen?bevoegdGezag=…` heeft een veld **`citeerTitel`** (hoofdletter
T). Gemeten op productie-API, 2026-08-07:

| officieleTitel (ingekort) | citeerTitel |
|---|---|
| Besluit van 3 juli 2018, houdende regels over de kwaliteit van de fysieke leefomgeving … | `Besluit kwaliteit leefomgeving` |
| Besluit van 3 juli 2018, houdende regels over activiteiten in de fysieke leefomgeving … | `Besluit activiteiten leefomgeving` |
| Besluit van 3 juli 2018, houdende regels over bouwwerken in de fysieke leefomgeving … | `Besluit bouwwerken leefomgeving` |
| Besluit van 3 juli 2018, houdende procedurele regels … | `Omgevingsbesluit` |
| Regeling van de Minister voor Milieu en Wonen, … van 21 november 2019 … | `Omgevingsregeling` |

Over de hele Rijk-set (1968 regelingen doorzocht) heeft **27×** de citeerTitel
een andere waarde dan de officiële titel — naast de AMvB's ook bijvoorbeeld
"Voorbereidingsbesluit bodem Papendrecht" tegenover een generieke officiële
titel, en "Nationale Omgevingsvisie: Duurzaam perspectief voor onze
leefomgeving". Bij de overige regelingen is de citeerTitel leeg of gelijk; die
houden gewoon hun opschrift.

Het veld ontbreekt ook in de andere loaders die naar `p2p.regeling` schrijven
([ow_loader.py:261](../dso-loader/src/loaders/ow_loader.py),
[ontwerp_loader.py:503](../dso-loader/src/loaders/ontwerp_loader.py)).

### Waarom dit meer is dan cosmetiek

Minstens twee afnemers hebben onafhankelijk een eigen verkortings-truc gebouwd
op de lange titel:

- **OCD-viewer** — `korteRegelingnaam` in
  `frontend/src/app/core/tekst.ts`: pakt de haakjes-staart, maar alleen als die
  op een citeertitel lijkt. Nodig omdat van de 1977 titels er 14 op haakjes
  eindigen waarvan de helft toelichting is (`(incl. Stolpen)`, `(Noorderhaaks)`,
  `(NH.)`, `(2026)`).
- **omgevingsdocumentenregister.nl** — een vergelijkbare truc, volgens de
  eigenaar eveneens suboptimaal.

Elke afnemer die dit zelf oplost, doet het net iets anders. Dat hoort één keer
in de data te gebeuren.

## Doel

`p2p.regeling.citeertitel` bevat de échte citeertitel wanneer de bron die
levert, en anders het opschrift. Afnemers kunnen dan zonder heuristiek
`COALESCE(citeertitel, opschrift)` gebruiken.

## Aanpak

1. **Loader** — `find_regelingen` neemt `citeerTitel` mee in het dict dat het
   teruggeeft; de insert schrijft `citeertitel` los van `opschrift`, met
   terugval op de officiële titel als het veld leeg is. Idem in `ow_loader` en
   `ontwerp_loader`.
2. **Backfill** — script dat voor bestaande regelingen de citeerTitel ophaalt
   en bijwerkt. Alleen `citeertitel` aanraken, `opschrift` ongemoeid laten.
   Bereik: ~1977 regelingen, waarvan naar verwachting enkele tientallen een
   afwijkende waarde krijgen.
3. **Afnemers** — de viewer kan `korteRegelingnaam` dan schrappen en
   `citeertitel` tonen; omgevingsdocumentenregister idem.

Semantiek expliciet vastleggen: `opschrift` blijft de **officiële** titel (die
hoort in een documentenlijst), `citeertitel` is de **korte** vorm (die hoort in
bijschriften, chips en verwijzingen).

## Verificatie

- [ ] `SELECT count(*) FROM p2p.regeling WHERE citeertitel <> opschrift` > 25 na de backfill
- [ ] Het Bkl geeft `citeertitel = 'Besluit kwaliteit leefomgeving'`
- [ ] Regelingen zonder citeerTitel in de bron houden `citeertitel = opschrift` (geen NULL)
- [ ] Een verse load van één bronhouder vult het veld meteen goed (geen backfill nodig)
- [ ] `opschrift` is voor geen enkele regeling gewijzigd (diff vóór/na)
- [ ] De viewer toont dezelfde korte namen als nu, maar zonder `korteRegelingnaam`

---

## Citeertitel van het besluit (`p2pwijziging.besluit`)

> Status: Opgelost
> Datum: 2026-08-10

Hetzelfde patroon, één niveau lager, en met een grotere opbrengst.

Een ontwerpregeling draagt in de Presenteren-API **twee** titel-niveaus:

| veld | hoort bij | Putten |
|---|---|---|
| `opschrift` / `citeerTitel` (top-level) | de regeling | "Omgevingsplan gemeente Putten" |
| `besluitMetadata.citeerTitel` | het besluit dat deze versie veroorzaakt | "Wijziging omgevingsplan gemeente Putten t.b.v. ontwikkeling Stenenkamerseweg 38/38a" |

`ontwerp_loader` las alleen het top-level veld. Gevolg: `citeertitel` was op
regeling-niveau gevuld en daarmee **gelijk voor elk besluit op dezelfde
regeling**. Putten heeft drie lopende ontwerpen; in de viewer heetten die alle
drie "Omgevingsplan gemeente Putten". De bron-selector, de tour-header en de
renvooi-pills in de leestekst waren dus onderling niet te onderscheiden.

Reikwijdte, gemeten 2026-08-10 op de productie-API:

- **ontwerpregelingen**: 805 van de 1028 leveren `besluitMetadata`
- **besluitversies**: 0 van de 2812 — het veld staat niet op het
  `Besluitversie`-schema, en ook de detail-endpoint
  `/besluitversies/{technischId}` levert het niet. Daar valt de loader terug op
  regeling-niveau.

### Besluitversies: tweede bron — de Ontsluiten-API

Presenteren heeft de naam niet, maar de DSO wel. Het Omgevingsloket toont voor
besluitversies wél een besluitnaam, en haalt die uit de **Omgevingsinformatie
ontsluiten API v2** — een gepubliceerde API op dezelfde service-host, met eigen
OpenAPI-spec, pre-productie-omgeving en `apiKey`-security. Hetzelfde
volwassenheidsniveau als Presenteren v8; `document-viewer.dso.kadaster.nl/bff/…`
staat er letterlijk als één van de `servers` in.

```
GET https://service.omgevingswet.overheid.nl/publiek/omgevingsinformatie/api/ontsluiten/v2
    /documenten/{uriIdentificatie}
→ omgevingsdocumentMetadata.besluitCiteertitel
```

`uriIdentificatie` = onze `p2pwijziging.besluit.technisch_id`.
`besluitCiteertitel` is een eersterangs veld op de schema's `BesluitVersie`,
`OntwerpDocumentVersie` én `OmgevingsdocumentMetadata`.

Gemeten 2026-08-10 over onze 124 besluitversies:

| | met besluit-eigen naam | gelijk aan opschrift | leeg |
|---|---|---|---|
| besluitversies | **115 (93%)** | 9 | 0 |

Voorbeelden: "Elshagenweg 3 Wesepe" (Raalte), "Veegplan 2026-1" (Oude
IJsselstreek), "H23 - postzegelplan \"Landelijk gebied Ede, partiële herziening
2026, 1e ronde\"", "Wijziging omgevingsplan gebiedsontwikkeling 'Zwembad
Wervershoof'" (Medemblik) — waar Presenteren voor alle vier alleen
"Omgevingsplan gemeente X" geeft.

**Voor ontwerpen voegt Ontsluiten niets toe.** Gemeten op de 65 ontwerpen zonder
`besluitMetadata`: **0** krijgen daar alsnog een naam. Andersom geeft Ontsluiten
voor het Putten-ontwerp als `titel` juist de generieke regelingsnaam. Daarom
roept alleen `load_besluitversie` de tweede bron aan — ontwerpen zouden er
alleen een extra call per stuk aan overhouden.

**Twee bedieningsdetails.** Er is geen batch-route: `/documenten/_zoek` geeft op
een work- of uriIdentificatie alleen de geconsolideerde versie terug, niet de
besluitversies. En deze host rate-limit steviger dan de rest — 65 calls zonder
pauze leverde 52 fouten, met ~0,4 s ertussen nul. Vandaar `time.sleep(0.4)` in
het backfill-script.

### Doorgevoerd

1. **Loader** — in [ontwerp_loader.py](../dso-loader/src/loaders/ontwerp_loader.py):
   `_besluit_citeertitel()` voor ontwerpen (eerst `besluitMetadata.citeerTitel`,
   dan het top-level veld) en `_ontsluiten_citeertitel()` voor besluitversies.
   Die tweede is **best-effort**: bij een fout of time-out `None` en terugval op
   de regeling-citeertitel — een besluitnaam is een siersel, geen dragend
   gegeven, en één hikkende API mag geen load stukmaken. Beide UPSERTs verversen
   `citeertitel` nu ook in de `ON CONFLICT DO UPDATE`; dat ontbrak, waardoor een
   herload de oude waarde had laten staan.
2. **Backfill** —
   [scripts/backfill_besluit_citeertitel.py](../dso-loader/scripts/backfill_besluit_citeertitel.py),
   `--soort ontwerp|besluitversie|beide`. Raakt uitsluitend de
   `citeertitel`-kolom, droogloop by default. Resultaat op lokaal én prod:
   224 van de 321 ontwerpen en 116 van de 124 besluitversies bijgewerkt.
   Onderscheidend van het opschrift: ontwerpen 239 (was ~15), besluitversies
   115 (was 3).
3. **Semantiek** — `COMMENT ON COLUMN` op beide kolommen, in `ddl.py` en als
   losse migratie
   ([2026-08-besluit-citeertitel-commentaar.sql](../dso-loader/scripts/2026-08-besluit-citeertitel-commentaar.sql)).
4. **API** — `/v1/viewer/regeling/{expression}/wijzigingen` geeft `citeertitel`
   mee per bron.
5. **Viewer** — `besluitNaam()` in `wijziging.model.ts` (citeertitel, terugval
   op opschrift), gebruikt door de bron-selector, de tour-header en de
   renvooi-bron-pill. `MetBron.bronOpschrift` heet daarom nu `bronNaam`.

### Verificatie

- [x] De drie Putten-ontwerpen hebben elk een eigen citeertitel in
      `p2pwijziging.besluit`
- [x] `/v1/viewer/regeling/…/wijzigingen` geeft ze alle drie verschillend terug
- [x] Besluitversies dragen hun eigen besluitnaam: Raalte = "Elshagenweg 3
      Wesepe", niet "Omgevingsplan gemeente Raalte"
- [x] Geen enkele besluitversie op NULL of lege string na de backfill
- [x] 328 viewer-tests groen, 69 loader-tests groen
