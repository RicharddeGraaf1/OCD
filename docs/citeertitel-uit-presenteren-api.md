# Citeertitel uit de Presenteren-API overnemen

> Status: Open
> Datum: 2026-08-07

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
