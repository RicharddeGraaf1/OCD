# Uitgewerkt voorbeeld — regels op een adres

> Van een adres naar de regels die daar gelden, met de volledige call-keten en de
> orde van grootte van het aantal requests. Hoort bij `/llms.txt`.

Dit is de meest gestelde vraag aan het stelsel en tegelijk de vraag waarop clients het
vaakst improviseren — meestal omdat ze de locatie-ingang niet kennen en dan zelf gaan
zoeken. Zoeken is duur.

## Wat het kost als je improviseert

Een agent zonder dit recept doet meestal dit: alle regelingen van de gemeente ophalen,
van elk de volledige documentstructuur, alle annotaties zonder locatiefilter, en
vervolgens elke locatie-identificatie apart als geometrie ophalen om zelf te bepalen
welke het adres raken. Daarna nog een keer hetzelfde voor het oude regime.

Dat is voor één adres al snel **enkele duizenden requests** en tientallen megabytes,
waarvan het overgrote deel wordt weggegooid. Alleen al de annotaties van één
omgevingsverordening leveren zonder `locatieSelectie=primair` ruim tienduizend
locaties op, tegen ruim honderd mét.

De keten hieronder doet hetzelfde werk in **ongeveer vijf tot tien requests**.

## De keten

### Stap 1 — Adres naar coördinaat (1 call, cachebaar)

Niet het DSO. Gebruik de PDOK Locatieserver:

```
GET https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?q=<adres>
```

Neem de RD-coördinaat (EPSG:28992) uit het resultaat. Cache het antwoord op de
genormaliseerde adresvraag — adressen verplaatsen niet.

Let op de rolverdeling: de geocode bepaalt *waar* je kijkt, maar is nooit zelf een
antwoord. Toon nooit de geocode-positie als was het een positie uit het stelsel.

### Stap 2 — Coördinaat naar documenten (1 call)

Dit is de stap die de meeste clients missen. **Ontsluiten v2 zet een coördinaat om in
de documenten die daar gelden**, en levert daarbij zowel de
Omgevingswet-documenten als de nog geldende plannen uit het oude Wro-regime. Je hoeft
die twee werelden dus niet zelf bij elkaar te zoeken, en je hebt geen omweg langs het
bevoegd gezag nodig.

```
POST /publiek/omgevingsinformatie/api/ontsluiten/v2/documenten/_zoek?size=200
Content-Crs: http://www.opengis.net/def/crs/EPSG/0/28992

{"geometrie": {"type": "Point", "coordinates": [161299.196, 385714.637]}}
```

De body kent naast `geometrie` de velden `_find`, `bestuurslaag`,
`regelgevingOfOverig` en `identificaties`; als queryparameter zijn `geldigOp`,
`beschikbaarOp`, `inclusiefToekomstigGeldig`, `page`, `size` en `_sort` beschikbaar.

Vijf dingen om hier goed te doen:

**Zet `size` expliciet.** De default is **20**. Op een gemiddeld punt gelden er
40 tot 50 documenten, dus met de default mis je stilzwijgend de helft — zonder
foutmelding, want een eerste pagina van 20 is een geldig antwoord.

**Stuur de `Content-Crs`-header mee.** Die is verplicht zodra je een geometrie
meegeeft, en er is precies één toegestane waarde:
`http://www.opengis.net/def/crs/EPSG/0/28992`. RD dus, geen WGS84.

**Rond je coördinaten af op drie decimalen.** Meer decimalen levert een 422
"Geometrie heeft meer dan 3 decimalen" op. Een geocoder geeft er zo vijftien.

**Filter zelf op wat je zoekt.** Je krijgt de volledige stapel terug: het
omgevingsplan én de provinciale omgevingsverordening, de waterschapsverordening,
de rijks-AMvB's, tientallen programma's en de nog geldende Wro-plannen. Dat is
correct — dat geldt daar allemaal — maar zelden is dat allemaal je vraag.

**Ga zuinig om met deze API.**

Ontsluiten verdraagt aanzienlijk minder belasting dan de andere API's van het
stelsel. Eén call per locatievraag is precies waarvoor hij bedoeld is; hem als
bulk-ingang gebruiken is dat niet — zie [Varianten](#varianten) voor de route die je
bij massaverwerking neemt.

Onthoud per document het `type`: dat bepaalt in stap 3 welk endpoint je nodig hebt, en
of het document via Presenteren of via IHR verder gaat.

> De volledige parameterlijst staat in de OpenAPI-spec, die publiek en zonder sleutel
> te lezen is op `/publiek/omgevingsinformatie/api/ontsluiten/v2/openapi.json` en
> gelinkt staat vanuit het
> [api-register](https://developer.omgevingswet.overheid.nl/api-register/). Neem hem
> daar op als je meer nodig hebt dan de velden hierboven.

### Stap 3 — De regels bij die documenten (1 call per relevant document)

Ontsluiten vertelt je wélke documenten gelden. Voor wat er in staat over déze locatie
ga je naar de API die de inhoud levert.

Voor Omgevingswet-documenten is dat Presenteren v8, met de geometrie opnieuw in de
body zodat het stelsel het filteren doet en niet jij:

```
POST /regelingen/{uriId}/regeltekstannotaties/_zoek?locatieSelectie=primair
     body: GeoJSON-punt in EPSG:28992
```

Drie dingen om goed te doen:

**Kies het juiste endpoint op basis van het documenttype.** Documenten met
artikelstructuur (omgevingsplan, omgevingsverordening, waterschapsverordening,
projectbesluit, AMvB) gebruiken `regeltekstannotaties`. Documenten met
vrijetekststructuur (omgevingsvisie, programma, instructie) gebruiken
`divisieannotaties`. Het verkeerde endpoint kiezen levert geen bruikbaar antwoord op
en dus een tweede call.

**Zet `locatieSelectie=primair`.** Je krijgt dan gebiedengroepen met een eigen,
geaggregeerde geometrie-identificatie in plaats van alle individuele gebieden. Regels,
activiteiten, gebiedsaanwijzingen en tekstdelen blijven identiek; alleen de
locatie-ruis verdwijnt.

**Encodeer het pad goed.** In regelingen-paden worden zowel `/` als `-` vervangen door
`_`. Alleen de slashes vervangen geeft 404 op elke regeling met een datum- of
UUID-segment — en een 404 die je vervolgens retryt, is verspilling in zuivere vorm.

Voor Wro-plannen uit stap 2 ga je in plaats daarvan naar IHR
(`ruimtelijke-plannen/api/opvragen/v4`), met een eigen sleutel.

### Stap 4 — De tekst erbij (1 call per geraakt document, of minder)

Je hebt nu de OW-objecten en de wId's van de tekstelementen waaraan ze hangen. Haal
niet de hele documentstructuur op, maar de subtree:

```
GET /regelingen/{uriId}/documentstructuur/{wId}
```

Bij meerdere treffers binnen hetzelfde document: haal de dichtstbijzijnde
gemeenschappelijke subtree één keer op in plaats van elk artikel apart.

### Stap 5 — De pons: op deze route niets te doen (0 calls)

De Omgevingswet heeft de bestemmingsplannen niet in één klap vervangen. Voor het deel
van het grondgebied dat nog niet is overgezet, geldt het oude plan nog steeds. De pons
is het OW-object dat aangeeft welk deel al wél is overgezet — een geometrische
operatie, geen vlag per plan: een bestemmingsplan kan gedéeltelijk weggeponst zijn.

**Op deze route hoef je daar niets mee.** De Wro-plannen die je in stap 2 terugkrijgt
zijn al pons-gecorrigeerd: een plan dat op het bevraagde punt is weggeponst, zit niet
in het antwoord. Bouw dus geen eigen correctie — je zou een correctie op een correctie
leggen.

Zelf toetsen is alleen nodig als je je voorraad níet via de locatie-ingang opbouwt
maar via de lijst-sweep (zie [Varianten](#varianten)). Dan geldt:

```
effectieve Wro-dekking = plangebied MINUS pons-geometrie
```

De pons-geometrie zit in het omgevingsplan en komt mee met `_expand=true` op de
regeling-detailcall. Een boolean per plan geeft het verkeerde antwoord — de
granulariteit zit op puntniveau.

### Stap 6 — Geometrie, alleen als je hem echt toont (0 calls voor een ja/nee-vraag)

Voor de vraag *welke regels gelden hier* heb je geen geometrie nodig — het stelsel
heeft de punt-in-vlak-toets al gedaan. Haal geometrie alleen op als je hem gaat
tékenen:

```
GET /publiek/omgevingsdocumenten/api/geometrieopvragen/v1/geometrieen/{uuid}
    ?crs=http://www.opengis.net/def/crs/EPSG/0/28992
```

Dedupliceer eerst: dezelfde geometrie-identificatie hangt aan tientallen objecten.

## Telling

| Stap | Calls | Cachebaar |
|---|---|---|
| 1. Adres → coördinaat | 1 | ja, permanent |
| 2. Coördinaat → documenten (Ontsluiten) | 1 | kort; dit is de verse vraag |
| 3. Regels per document | 1 per relevant document | nee |
| 4. Tekst-subtree | 1 per geraakt document | ja, per expressie |
| 5. Pons-toets | 0 — al toegepast | n.v.t. |
| 6. Geometrie | 0, tenzij je tekent | ja, per identificatie |
| **Totaal** | **± 5–10** | |

De grootste winst zit in stap 2. Zonder die ingang moet je het bevoegd gezag afleiden
uit bestuurlijke grenzen, de regelingenlijst per bestuurslaag ophalen, kandidaten
filteren, het oude regime apart bevragen én zelf tegen de pons toetsen — vijf stappen
die nu in één call zitten, en precies de stappen waarin je je kon verrekenen.

## Varianten

### Massaverwerking: niet via Ontsluiten

Wil je niet één adres maar een hele voorraad — een dataset opbouwen, een gemeente
doorrekenen, een index vullen — draai de richting dan om. Ontsluiten is de zwakst
geschaalde API van het stelsel en niet bedoeld als bulk-ingang.

Sweep dan de regelingenlijst (`GET /regelingen`, `size=200`, ongeveer tien calls voor
de landelijke voorraad), haal per regeling eenmalig de annotaties op, en doe de
locatietoetsen daarna lokaal. Eén keer ophalen, daarna onbeperkt bevragen. Zie de
deltasectie in `/llms.txt` voor hoe je die voorraad bijhoudt zonder alles opnieuw op
te halen.

De vuistregel: **Ontsluiten voor een vraag, de lijst-sweep voor een voorraad.**

### "Mag ik hier een dakkapel bouwen?"

Dit is géén vraag aan Presenteren of Ontsluiten. De toepasbare regels zijn een apart
artefact met een eigen levenscyclus:

1. `POST /activiteiten/_zoek` op de RTR, met het bestuursorgaan en eventueel de
   locatie. **Gebruik de kale organisatiecode**: `0363`, niet `gm0363` — met prefix
   krijg je een geldige 200 met nul resultaten en geen enkele aanwijzing dat je vraag
   fout was.
2. `datum` op vandaag zetten. De RTR is geldigheidsgestuurd; een hardgecodeerde
   peildatum bevriest je beeld zonder dat er iets klaagt.
3. De bijbehorende beslisregels als DMN ophalen uit de STTR en lokaal evalueren.

De vergunningcheck in het Omgevingsloket nabootsen door de webflow te scripten is
duurder en levert je minder op: als DMN heb je dezelfde regels deterministisch en
herhaalbaar.

### "Wat gold hier op 1 januari 2023?"

Gebruik de temporele parameters (`geldigOp`, `inWerkingOp`, `beschikbaarOp`) in plaats
van zelf versies te verzamelen, en `/regelingen/{uriId}/voorkomens` voor de
versiehistorie van één regeling. Let op dat de drie tijdsassen verschillende vragen
beantwoorden — zie `/llms.txt`.

### "Is er iets veranderd sinds gisteren?"

Niet per adres pollen. Sweep één keer de landelijke regelingenlijst, vergelijk de
`expressionId`'s met wat je hebt, en herbereken alleen voor de gewijzigde regelingen.
Zie de deltasectie in `/llms.txt` — met name de waarschuwing dat een watermark op het
registratietijdstip stil dingen mist.

## Samengevat

De vier beslissingen die het verschil maken tussen vijf en vijfduizend requests:

1. **Begin bij de locatie-ingang.** Ontsluiten v2 geeft je in één call de documenten
   uit beide regimes, met de pons-correctie al toegepast. Zelf het bevoegd gezag
   afleiden, twee bronnen combineren en de pons naberekenen is werk dat het stelsel al
   voor je doet — en waarin je alleen maar fouten kunt introduceren.
2. **Laat het stelsel filteren.** Geo-zoeken met een geometrie in de body, niet alles
   ophalen en zelf toetsen.
3. **Zet `locatieSelectie=primair`.** Zonder die parameter haal je twee ordes van
   grootte meer locaties op dan je gebruikt.
4. **Kies de juiste richting voor je doel.** Eén vraag: Ontsluiten. Een voorraad: de
   lijst-sweep, één keer, en daarna lokaal.
