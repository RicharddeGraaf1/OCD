# Wro in Regels op maat — herstel van de planteksten en koppeling per punt

*Opgesteld 2026-10-23 (avond), op verzoek van de gebruiker: "ik wil dat we ook Wro
gaan doen". Uitgevoerd in dezelfde nacht, zonder tussentijds overleg. Wat
uitgevoerd is en wat niet, staat onderaan bij **Stand van uitvoering**.*

## Aanleiding

In de RoM-benchmark (`omgevingsdocumentenregister.nl/tests/rom-benchmark/`) scoren de
Wro-vragen bijna niets: het plan staat niet bij het punt (Earnewâld 2013, Gaaf
Gelderland: 0 artikelen), of het artikel ontbreekt (Hilversum *Binnenstad 2013*
art. 11.1, Groningen *Binnenstad* art. 4.1). Vault G-166 noemde dit "Wro-plannen
zonder tekst" en liet open of het bron of loader was.

## Diagnose (gemeten 2026-10-23)

1. **De loader stopt na 100 teksten per plan.** `ihr_loader.load_teksten_for_plan`
   leest `page.totalElements`, maar IHR v4 levert geen `page`-blok, alleen
   `_links.next`. `total` wordt 0 en de lus breekt na pagina 1.
   Hilversum *Binnenstad 2013*: 770 teksten bij IHR, 22 in OCD.
2. **De sleutel is niet uniek.** `wro.wro_tekst_object.identificatie` is
   `PRIMARY KEY`, maar IHR-tekst-id's als `NL.IMRO.PT.regels.s1266` zijn alleen binnen
   één plan uniek. `ON CONFLICT (identificatie) DO NOTHING` gooit dus stil de teksten
   weg van elk volgend plan met dezelfde id's. Earnewâld 2013: 0 teksten.
3. **De koppeling tekst ↔ bestemming wordt niet opgeslagen.** IHR geeft per tekst
   `_links.bestemmingsvlakken` (en gebiedsaanduidingen, functieaanduidingen,
   bouwaanduidingen, maatvoeringen, figuren), met dezelfde id's als `wro.planobject`.
   `planobject.artikelnummer` is in 5.759 van 1,7 mln enkelbestemmingen gevuld.
   Daardoor levert `/v1/viewer/regelmix/document` voor Wro *alle* teksten van het plan,
   niet die van de bestemming op het punt.

Omvang landelijk (lokaal): 59.968 bestemmings-, wijzigings-, uitwerkings- en
inpassingsplannen; 9.460 zonder tekst, 43.675 met minder dan 30 tekstobjecten.
Steekproef 30 plannen bij IHR: gemiddeld 165 teksten (mediaan 146, max 741),
~187 KB inhoud per plan. Volledig: ~8–10 mln rijen.

## Aanpak

Nieuwe tabellen **naast** de oude (de oude blijft ongemoeid; converter, vectorlaag en
API lezen hem nog):

- `wro.wro_tekst_object_v2` — sleutel `(instrument_idn, identificatie)`, verder
  dezelfde kolommen.
- `wro.wro_tekst_link` — `(instrument_idn, tekst_id, planobject_id, soort)`, uit
  `_links` van elke tekst.
- `wro.wro_tekst_laadstand` — per plan: aantal teksten, links, moment, fout.
  Maakt de lading hervatbaar.

### Fasen

| # | Wat | Waar | Risico |
|---|---|---|---|
| 1 | Loader: paginering via `_links.next`, sleutel per plan, links opslaan, hervatbaar en parallel | `dso-loader` | geen (nieuwe tabellen) |
| 2 | Lokaal laden: eerst de benchmarkplannen, dan alle niet-ontwerp-plannen | lokale DB | geen |
| 3 | API: Wro-teksten per punt — teksten die via `wro_tekst_link` aan een planobject op het punt hangen, plus hun onderliggende leden; de inleidende regels (hoofdstuk 1) erbij. Valt terug op het oude gedrag als een plan nog niet in v2 staat | `ocd-api` | laag (terugval) |
| 4 | Benchmark lokaal: Wro-vragen op document en artikel | tests | geen |
| 5 | Onderwerp-indeling voor Wro-artikelen (bestemmingsnaam/artikelkop → categorie met dezelfde regels) + tegels in RoM | `dso-loader`, `ocd-api`, register | middel (frontend) |
| 6 | Productie | Railway | **hoog: volume 82,2 / 100 GB** |

### Productie: wat wel en wat niet

De volledige lading (enkele GB plus indexen en WAL) past niet veilig in de 17,8 GB
die op de Railway-volume vrij is, en een volle volume legt de database plat.
Vergroten kan alleen in het dashboard. Daarom:

- **Wel**: de API-aanpassing (fase 3), omdat die terugvalt op het oude gedrag zolang
  een plan niet in v2 staat; en hooguit een kleine deelset van de data, met een vooraf
  gemeten omvang.
- **Niet**: de volledige lading. Die staat klaar als script, voor ná het vergroten van
  de volume.

## Stand van uitvoering

*(wordt tijdens de nacht bijgewerkt)*
