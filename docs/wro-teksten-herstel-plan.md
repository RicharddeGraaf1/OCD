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
| 6 | Productie | Railway | **hoog: volume 82,2 / 100 GB** (vergroot naar 150 GB op 24-10) |

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

*Bijgewerkt in de nacht van 23 op 24 oktober 2026.*

| # | Wat | Stand |
|---|---|---|
| 1 | Loader v2 (`laad_wro_teksten_v2.py`) | **klaar** — paginering via `_links.next`, sleutel per plan, koppelingen, hervatbaar, 8 workers / 15 req/s, 0 fouten |
| 1b | HTML-regels (`laad_wro_regels_html.py`) | **klaar** — Word-export (`<p class=…>`), div/h-pagina's en `_Index.html` (wortelpagina `_0000` als die ≥80% van de artikelen bevat, anders alle onderdeelpagina's ontdubbeld); koppeling via exacte bestemmingsnaam |
| 2 | Lokaal laden | **loopt / klaar** — zie de tellingen hieronder |
| 3 | API | **klaar en live** (`f6ec188` op `main`): regelmix telt v2; regelmix/document geeft per punt de teksten van de bestemmingen en aanduidingen op dat punt plus de planbrede regels; artikelen onder *Bestemmingsregels* zonder koppeling vallen weg (ze horen bij een bestemming die hier niet ligt); rijen dragen `categorie`/`subcategorie`; wro-detail leest v2. Zonder v2 voor een plan: oud gedrag. 84 bestaande tests + 4 nieuwe (`TestWroV2OpPunt`) groen |
| 4 | Benchmark | **klaar** — lokaal én prod: Doc 33 → **38**/40, Artikel 18 → **20**/22; Wro-vragen 5, 7–9, 11–14, 39 op prod gelijk aan lokaal (24-10, 11:32) |
| 5 | Wro-onderwerp-indeling | **data + API klaar** (`bouw_wro_indeling.py`, `WRO_HOOFDGROEP`); **frontend klaar maar niet live**: branch `wro-onderwerpen` in het register (`1eab69a`, niet gepusht) |
| 6 | Productie | **volledig live** (24-10, 07:55–11:31): volume eerst 100 → 150 GB (dashboard), daarna `--alle --max-mb 12000 --ja`: 48.161 plannen, 7.820.415 teksten, gelijk aan lokaal; 216 min; prod-DB 74 → 84 GB; ANALYZE op de vier tabellen. Eerder die nacht: deelset van 852 plannen |

### Gemeten — eindstand lokaal (24-10, 02:30)

| | Oud (`wro_tekst_object`) | Nieuw (v2) |
|---|---|---|
| Teksten | 799.183 | **7.820.415** |
| Koppelingen tekst → planobject | — | 2.756.931 |
| Plannen met tekst (niet-ontwerp, 52.219) | — | 48.161 (44.801 IHR, 3.360 HTML) |
| Plannen zonder tekst | — | 4.058: geen regelsdocument 2.998 (vooral oude "gemeentelijk plan"), alleen pdf 577, structuur niet herkend 416, index zonder herkende onderdelen 67 |
| Met onderwerp (`wro_artikel_indeling`) | — | 3.739.730 van 7.805.240 tekstdelen (48%; de rest: begrippen, meetregels, algemene regels, toelichting, bijlagen) |
| Omvang | 771 MB | v2 6,6 GB + links 1,1 GB + indeling 2,1 GB ≈ **9,7 GB** |

- **Juistheid per punt** (80 willekeurige enkelbestemmingsvlakken, punt binnen het vlak):
  bij alle 79 vlakken met een koppeling komt de tekst van die bestemming mee (74 IHR,
  5 HTML); 1 vlak heeft geen gekoppelde tekst.
- Doorlooptijd: IHR-lading 51.262 plannen in 119 min (7,2 plan/s, 0 fouten); HTML
  2 rondes; indeling 89 min met 8 processen.
- Bekende zwakte HTML: indexplannen met een eigen paginanaamgeving worden soms maar
  deels gelezen (Noranjewoud: 5 van ~26 artikelen); bij een plan met al IHR-teksten
  vervangt HTML die alleen als het minstens twee keer zoveel onderdelen oplevert.

### Gemeten tijdens de bouw

- Hilversum *Binnenstad 2013*: 770 teksten (was 22); op 's-Gravelandseweg 9 234 van de
  617 tekstdelen met inhoud, waaronder art. 11.1 *Dienstverlening*.
- Earnewâld 2013 (HTML): 46 artikelen, 151 leden, 183 subleden; art. 29 *Wonen - A1*
  gekoppeld aan 68 vlakken.
- Koppelingsdekking (aandeel bestemmingsvlakken met minstens één gekoppelde tekst):
  IHR-plannen enkelbestemming 96,6%, dubbelbestemming 95,1%; HTML-plannen 83,3% / 91,3%.
- Plannen zonder objectgerichte teksten: oude "gemeentelijk plan"-plannen hebben geen
  regelsdocument (niet te halen); van de bestemmingsplannen met een HTML-document lukt
  het merendeel (herkansing: 554 van 1.029 kandidaten; 89 onleesbaar, 62 index zonder
  onderdelen, 31 alleen pdf).
- Omvang: ~158 KB per plan (teksten + koppelingen), volledig ~10 GB met HTML en indeling.

### Openstaand

1. ~~Volume vergroten en volledige overzet~~ — gedaan 24-10. Bij een volgende Wro-sync per
   bronhouder of plan opnieuw `wro_v2_naar_productie.py` (idempotent per plan).
   Kanttekening: ~0,7% van de koppelingen (steekproef 2000) wijst naar een bestemmingsvlak dat
   lokaal én op prod ontbreekt in `wro.planobject`; die tekst komt dan niet per punt. Een
   laadgat in de planobjecten, niet in de overzet.
2. **Frontend**: branch `wro-onderwerpen` bekijken en mergen. Ontwerpvraag: tellen
   Wro-artikelen mee in de tegels bovenaan, of alleen als chips per Wro-document?
3. **Sync**: de runbook-stap voor Wro-teksten moet `laad_wro_teksten_v2.py` (+ HTML +
   `bouw_wro_indeling.py`) worden; de oude `ihr_loader` schrijft nog naar de oude tabel.
4. **Vraagroute** (`/v1/regelteksten-bij-vraag`) leest de Wro-teksten nog niet; die
   endpoint deelt de omgevingsbot, dus niet ongezien aangepast.
5. **Onderwerp-gaten** in `WRO_HOOFDGROEP`/REGELS: o.a. *Historische buurt*, *Waarde -
   Stedenbouwkundige karakteristiek* blijven niet ingedeeld.
