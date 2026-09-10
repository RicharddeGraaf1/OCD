# Vrijetekst-gaten: register en herstelplan

> Opgesteld 2026-09-10, naar aanleiding van de meting van 537 omgevingsvisies en
> programma's tegen de Annotatierichtlijn Omgevingsvisie en Programma v0.9.2
> (annotatieconformiteit.nl).

Bij het toetsen van vrijetekstdocumenten bleek een reeks annotaties niet te
meten. Bij nader onderzoek is dat maar deels een gebrek aan brondata: **in drie
van de zes gevallen levert de bron het wél en laat onze eigen loader het
vallen.** Dit document markeert de gaten, beschrijft de herstelling, en legt
vast hoe ze bij elke sync bewaakt worden.

## Waarom dit register bestaat

Een ontbrekende annotatie ziet er in de database precies zo uit als een
annotatie die er nooit was. `p2p.tekstdeel.divisie_wid` stond 2.934 keer op een
lege string; dat leek "annotatie zonder verwijzing", maar het zijn annotaties op
divisieniveau waarvan de loader het veld niet uitleest. Zulke stilte is de
gevaarlijkste soort: hij vertaalt zich naar een oordeel over het bevoegd gezag
terwijl het een fout van ons is.

## Gatenregister

| ID | Gat | Aard | Status |
|---|---|---|---|
| **V-1** | `idealisatie` wordt niet opgeslagen | loaderverlies — API én XML leveren het | te herstellen |
| **V-2** | `divisieRef` wordt genegeerd in de API-route | loaderverlies — 2.934 annotaties met lege verwijzing | te herstellen |
| **V-3** | `divisies` en `divisieteksten` (met `wId`) worden weggegooid | loaderverlies — dit is de brug IMOW ↔ STOP | te herstellen |
| **V-4** | illustratie-metadata "niet ontsloten" | **onjuiste diagnose** — staat al in `tekst_element.inhoud` | geen werk |
| **V-5** | `p2p.besluit` is leeg | tabel bestaat, wordt nergens gevuld | gemarkeerd, buiten scope |
| **V-6** | `locatie.bron` is overal NULL | herkomst niet vastgelegd | lage prioriteit |
| **V-7** | health-laag kent geen vrijetekstsignaal | `mv_bronhouder_health` meet alleen artikelstructuur | te herstellen |
| **V-8** | geen gerichte herlaadroute voor vrijetekst | `herlaad-annotaties-stale` loopt via `juridische_regel` | te herstellen |
| **V-9** | `gebiedsaanwijzing.symboolcodes` wordt niet opgeslagen | loaderverlies | gemarkeerd, lage prioriteit |
| **V-10** | `kaart.nummer` en `kaart.uitsnede` worden niet opgeslagen | loaderverlies | gemarkeerd, lage prioriteit |
| **V-11** | ingetrokken tekstdelen blijven staan | de loader upsert alleen en verwijdert nooit | hersteld |

---

### V-1 — `idealisatie` wordt niet opgeslagen

De Presenteren-API levert per tekstdeel:

```json
"idealisatie": { "code": "…/concept/Indicatief", "waarde": "indicatief" }
```

`parse_tekstdelen` in `src/parsers/ow_xml.py` leest het al uit de XML en zet het
in het resultaat (met terugval op `"exact"`). Maar `p2p.tekstdeel` heeft geen
kolom, dus beide INSERT's laten het veld vallen.

Dat het om echte informatie gaat: van de omgevingsvisie van Waadhoeke staan
**alle 76 tekstdelen op "indicatief"**. Die hele visie is als indicatief bedoeld
en dat is nergens terug te vinden.

Herstel: kolom `idealisatie` op `p2p.tekstdeel`, met FK naar de bestaande
waardelijst `core.idealisatie`; beide loaders vullen hem.

### V-2 — `divisieRef` wordt genegeerd in de API-route

`load_divisieannotaties` leest `td.get("divisietekstRef", "")`. Een tekstdeel dat
een **divisie** annoteert heeft in plaats daarvan een `divisieRef`, en krijgt dus
een lege `divisie_wid`.

Gevolg: 2.934 van de 29.084 tekstdelen (10%) lijken nergens aan te hangen. Het
zijn annotaties op divisieniveau — precies waar richtlijn 3 van de
annotatierichtlijn voor waarschuwt, want de viewer toont ze niet bij de
onderliggende divisieteksten. Ze zijn nu niet te onderscheiden van een kapotte
verwijzing.

De XML-route heeft dit al goed (`ow_xml.py` probeert `DivisietekstRef` én
`DivisieRef`); alleen de API-route mist het.

Herstel: beide velden lezen, plus een kolom `divisie_soort` (`divisietekst` of
`divisie`) zodat het onderscheid expliciet in de data staat in plaats van
afgeleid te moeten worden uit de vorm van de identificatie.

### V-3 — de wId-brug wordt weggegooid

Dezelfde API-respons bevat twee collecties die de loader niet aanraakt:

```json
"divisieteksten": [ { "identificatie": "nl.imow-gm1949.divisietekst.76cd…",
                      "wId": "gm1949_…__div_o_2__div_o_4__content_o_23" } ],
"divisies":      [ { "identificatie": "nl.imow-gm1949.divisie.df71…",
                      "wId": "gm1949_…__div_o_4" } ]
```

Dat is exact de koppeling die ontbrak tussen de IMOW-annotatie en het
STOP-tekstelement. Zonder die brug kun je alleen op documentniveau tellen
(*hoeveel* divisieteksten zijn geannoteerd) en niet per divisietekst (*welke*).

Herstel: tabel `p2p.divisie` met `identificatie`, `wid`, `soort` en
`regeling_expression`. Daarmee wordt `p2p.tekstdeel` → `p2p.divisie` →
`p2p.tekst_element` een sluitende keten.

### V-4 — illustratie-metadata: onjuiste diagnose

Eerder vastgesteld als "wordt niet geladen". Dat klopt niet. De STOP-tekst wordt
integraal opgeslagen in `p2p.tekst_element.inhoud`, inclusief:

```xml
<Illustratie naam="…jpg" breedte="1386" hoogte="1386"
             uitlijning="start" formaat="image/jpeg" dpi="301"/>
```

7.996 tekstelementen in vrijetekstdocumenten bevatten een illustratie. Van 674
onderzochte illustraties heeft **674 een `dpi`, en 70 een `alt`** — 10%.

Er is dus geen loaderwerk nodig; de meting kan de opgeslagen XML lezen. Een
aparte tabel zou wel schelen in leestijd, maar is geen voorwaarde.

*Leerpunt:* "er is geen tabel voor" is geen bewijs dat de gegevens ontbreken.
Dit is de derde keer in dit onderzoek dat die aanname onjuist bleek — zie ook
V-1 (parser leverde het al) en de eerdere conclusie over `locatiegroep_lid`, waar
Gebiedengroepen hun eigen geometrie bleken te dragen.

### V-5 — `p2p.besluit` is leeg

De tabel is aangemaakt maar wordt door geen enkele loader gevuld; er is ook geen
kolom voor de wijzigingsmethode (renvooi / integrale tekstvervanging / intrekken
& vervangen). Buiten de scope van dit plan, wel gemarkeerd: het blokkeert elke
toets op hoe een document gewijzigd is.

### V-9 en V-10 — kleinere velden die dezelfde respons wél levert

Na het herstel van V-1 tot V-3 is de rest van de `divisieannotaties`-respons
nagelopen. Twee velden worden nog steeds niet bewaard:

| Bron | Veld | Opslag nu |
|---|---|---|
| `gebiedsaanwijzingen[]` | `symboolcodes` | `p2p.gebiedsaanwijzing` kent alleen identificatie, type, naam, groep, locatie |
| `kaarten[]` | `nummer`, `uitsnede` | `p2p.kaart` kent alleen identificatie en naam |

Geen van beide blokkeert op dit moment een richtlijn, vandaar de lage
prioriteit. `symboolcodes` is wel het overwegen waard: het zegt iets over hoe
een gebiedsaanwijzing in de viewer wordt verbeeld, en dat raakt aan richtlijn 21
over symbolen — al gaat die richtlijn over de geometrie en niet over de
symbolisatie.

Alle andere velden in de respons (`locaties`, `hoofdlijnen`, `tekstdelen`)
worden na dit herstel volledig geladen.

### V-11 — ingetrokken tekstdelen blijven eeuwig staan

Gevonden tijdens de verificatie van het herstel zelf: na het herladen van alle
565 regelingen hielden elf tekstdelen een lege `idealisatie`, terwijl de rest
netjes gevuld was. Alle elf hoorden bij Zuid-Holland.

De verklaring: die tekstdelen bestaan niet meer. De omgevingsvisie levert er nu
61 waar wij er 64 hadden, het omgevingsprogramma 134 tegen onze 142. Zuid-Holland
heeft ze ingetrokken, maar de loader deed uitsluitend upserts — een rij die de
bron niet meer noemt wordt nooit meer aangeraakt en verdwijnt dus nooit.

Dat is verraderlijker dan het lijkt. Zulke rijen tellen mee in elke dekkingsgraad
en in elke health-controle, en ze zijn niet te onderscheiden van rijen die de
loader per ongeluk oversloeg. Precies daarom vielen ze hier op: het nieuwe
`idealisatie`-veld bleef bij hen leeg omdat een herlading ze niet meer bereikt.

Herstel: `load_divisieannotaties` verwijdert na afloop de tekstdelen van deze
expressie die niet in de respons voorkwamen. Alleen wanneer de respons
daadwerkelijk tekstdelen bevatte — een lege respons kan ook een storing zijn, en
dan zou de opruiming de hele annotatievoorraad van een regeling wissen.

### V-6 — `locatie.bron` is overal NULL

Alle 321.269 locaties. Er is een werkbare omweg voor het toetsen van het
ambtsgebied (vergelijken met `core.gemeentegrens`), dus lage prioriteit.

---

## Herstel

### Stap 1 — schema

`scripts/2026-09-add-vrijetekst-annotatiedetails.sql`, en dezelfde statements
idempotent in `ddl.py`:

- `p2p.tekstdeel.idealisatie TEXT NULL REFERENCES core.idealisatie(code)`
- `p2p.tekstdeel.divisie_soort TEXT NULL` (`divisietekst` | `divisie`)
- `p2p.divisie (identificatie PK, wid, soort, regeling_expression)`

### Stap 2 — loaders

- `api_loader.load_divisieannotaties`: idealisatie lezen, `divisieRef` als
  terugval op `divisietekstRef`, en de collecties `divisies` en `divisieteksten`
  naar `p2p.divisie` schrijven.
- `ow_loader`: idealisatie meeschrijven (de parser levert hem al) en het soort
  vastleggen.

### Stap 3 — health-laag

`mv_bronhouder_health` en `v_data_health` krijgen vrijetekstkolommen, zodat een
terugval zichtbaar wordt zonder dat iemand ernaar zoekt:

- `vt_tekstdelen`, `vt_zonder_divisieref`, `vt_zonder_idealisatie`,
  `vt_pct_op_divisieniveau`

Een stijging van `vt_zonder_divisieref` na een sync betekent dat de loader weer
velden laat vallen. Dat is precies het signaal dat er tot nu toe niet was.

### Stap 4 — gerichte herlaadroute

`herlaad-vrijetekst` als CLI-commando: alle actieve `RegelingVrijetekst`, of een
selectie, opnieuw door `load_divisieannotaties`. Nodig omdat
`herlaad-annotaties-stale` via `juridische_regel` selecteert en vrijetekst daar
geen rijen heeft.

### Stap 5 — runbook

Opnemen in `docs/synchronisatie-runbook.md` als onderdeel van de p2p-fase, met
de health-controle in de verificatiestap.

## Verificatie na herstel

| Wat | Verwachting |
|---|---|
| `p2p.tekstdeel` met `idealisatie IS NULL` | 0 voor herladen regelingen |
| `p2p.tekstdeel` met lege `divisie_wid` | 0; de 2.934 krijgen `divisie_soort = 'divisie'` |
| `p2p.divisie` | ± 7.800 rijen (divisies + divisieteksten van 565 regelingen) |
| keten `tekstdeel → divisie → tekst_element` | sluitend voor de geladen regelingen |


---

## Uitvoering — 2026-09-10, 22:00–22:55

Herstel uitgevoerd en geverifieerd. Alle 565 actieve vrijetekstregelingen zijn
opnieuw geannoteerd via `herlaad-vrijetekst`, gevolgd door een tweede ronde met
`--alleen-onvolledig` voor de achterblijvers.

### Eindstand van de signalen

| | vóór | na |
|---|---|---|
| annotaties zonder divisieverwijzing | 2.934 | **0** |
| tekstdelen zonder idealisatie | 28.819 | **0** |
| tekstdelen zonder divisiesoort | 28.819 | **0** |
| regelingen zonder wId-brug | 564 | **0** |
| rijen in `p2p.divisie` | 1 | **28.825** |
| keten annotatie → divisie → tekstelement | niet mogelijk | **28.852 van 28.852 (100%)** |

### Wat er zichtbaar werd

- **2.934 annotaties op divisieniveau.** Exact het aantal dat eerder als lege
  verwijzing in de data stond — een sluitende bevestiging van de diagnose.
- **2.253 tekstdelen met een indicatieve begrenzing**, in 56 documenten.
  Die informatie bestond tot vandaag nergens in onze database.
- **35 documenten met nul annotaties.** Die kunnen per definitie geen wId-brug
  hebben; ze worden nu apart geteld als `zonder_annotaties` in plaats van als
  fout.

### Twee bijstellingen tijdens de uitvoering

1. **V-11 kwam pas boven door het herstel zelf.** Elf tekstdelen hielden een
   lege idealisatie omdat ze niet meer in de bron voorkomen. Zonder het nieuwe
   veld was dat nooit opgevallen — een ingetrokken rij is in de database niet te
   onderscheiden van een rij die de loader oversloeg.
2. **De verwachting dat `zonder_wid_brug` nul moet zijn was te grof.** Voor een
   document zonder annotaties klopt die niet. De view en het runbook maken nu
   onderscheid tussen foutsignalen en inhoudelijke tellingen.

### Doorwerking naar annotatieconformiteit.nl

Van de 29 richtlijnen zijn er nog vier onmeetbaar (R15, R21, R28, R29), tegen
twaalf ervoor. R8 ging van 502 documenten ongetoetst naar nul. R4 en R10 meten
sinds de brug per divisietekst in plaats van via twee tellingen naast elkaar.
