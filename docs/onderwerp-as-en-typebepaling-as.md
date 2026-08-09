# Onderwerp-as en typeBepaling-as — herbouw van de categorie-indeling

**Status**: LIVE sinds 2026-08-09 · **Datum**: 2026-08-07, bijgewerkt 2026-08-09
**Onderbouwing**: vault-analyse `Onderwerp-as en typeBepaling-as in de machinale
categorie-indeling` (OmgevingswetKnowledgeBase), gaps G-115/G-116.

---

## 1. Wat er mis is

De aanleiding: artikel 22.96 van gm0358 (*geur van landbouwhuisdieren en
paarden en pony's*) staat in het register onder **milieu › Tanken en vloeibare
brandstoffen**.

Drie fouten stapelen op elkaar.

**(a) De taxonomie kent één as waar een bepaling er twee heeft.** Elke bepaling
heeft een *onderwerp* (geur, geluid, bodem) én een *typeBepaling* (toepassingsbereik,
oogmerk, meldingsplicht, maatwerkvoorschrift). `v2a.categorie` kent alleen
onderwerpen, dus alles wat de clustering over de typeBepaling-as ontdekt moet als
onderwerp gelabeld worden. Soms eerlijk (*Maatwerkvoorschriften*,
*Meldings- en gegevensverstrekking*), soms niet (*Tanken*: 74% van de 34.220
toewijzingen is de zin "deze paragraaf is (niet) van toepassing", 1,7% noemt
brandstof, 86% hangt onder het artikelopschrift `Toepassingsbereik`).

Dit is geen incident maar een klasse: **~26%** van de 737.911 toewijzingen zit
in zuivere typeBepaling-categorieën, met `procedures` en de mengvormen erbij ~40%.
En het herhaalt zich per definitie, omdat de bruidsschat woordelijk identiek in
~340 gemeenten staat en dus de strakste clusters in het corpus vormt.

**(b) Er wordt geclassificeerd op de verkeerde tekst.** De wetgever haalt het
onderwerp bewust uit de losse bepaling en zet het in het opschrift erboven.
**64%** van de geclassificeerde leden is lid 2 of later en is daarmee per
constructie onderwerploos. `build_categorie.py` embedt precies die
onderwerploze tekst.

**(c) De eenheid klopt niet.** Classificatie op lidniveau, weergave op
artikelniveau. Bij **30,9%** van de meerledige artikelen landen de leden in
verschillende thema's; de viewer smeert dat dicht door elk lid het hele artikel
te laten kleuren.

Bijkomend: `build_categorie.py` regel ~154 is een kale `argmax` met ondergrens
`COVER_SIM = 0.50`, terwijl de nomic-cosine tussen willekeurige juridische
zinnen al rond 0,80–0,85 ligt. Bij **45%** van de toewijzingen is de marge
tussen nr. 1 en nr. 2 kleiner dan 0,01.

## 2. De meting die de oplossing kiest

IJkpunt: `p2p.juridische_regel.thema` (menselijke IMOW-annotatie), leden met
precies één thema. 12.184 stuks, ontdubbeld op de kale tekst (anders meet je
hoe vaak de bruidsschat gekopieerd is), labels genormaliseerd, 4.000 gebruikt,
11 klassen. Centroïde uit de trainhelft, meten op de testhelft, vijf splits.

| variant | juist | macro |
|---|---:|---:|
| **E — volledig opschriftpad + artikelopschrift** | **82,7% ±0,6** | **86,4% ±1,3** |
| C — volledig opschriftpad | 82,6% ±0,6 | 86,2% ±1,4 |
| F — pad + artikelopschrift + artikeltekst | 81,4% ±0,7 | 84,8% ±1,5 |
| D — pad zónder generieke toplaag | 75,0% ±0,4 | 79,3% ±1,0 |
| A — artikeltekst (huidig) | 55,3% ±1,0 | 58,0% ±1,3 |
| B — alleen het diepste opschrift | 47,0% ±0,4 | 55,8% ±1,4 |
| *ondergrens (grootste klasse raden)* | *44,4%* | — |

Consequenties voor het ontwerp:

- **Het volledige pad, plat, inclusief de generieke toplaag.** Toplaag weglaten
  kost 7,6 punt — geen diepteweging dus.
- **Niet alleen het naaste opschrift.** Dat is slechter dan niets doen (47,0%
  tegen 44,4%), want bij 26,5% van de leden is het diepste kopje zelf een
  typeBepaling-woord.
- **De tekst niet aanplakken.** Kost 1,4 punt; gepaard getoetst met McNemar
  (z=5,65, 378 vs 237 flips over 5 splits). Ruis-controle (eigen tekst
  vervangen door die van een willekeurig ander artikel → 78,3%) laat zien dat
  het geen inhoudsprobleem is maar tokenmassa: een lid is gemiddeld 332 tekens,
  een pad ~120. Mengen op α=0,9 geeft 82,8% — +0,2 punt, binnen de spreiding,
  en niet de moeite van 738k ledenteksten blijven embedden waard.

Verworpen door meting: top-N **losse artikelopschriften** met de hand cureren
(top-1000 dekt 15,4%; 18.283 unieke opschriften over 151.552 artikelen, met
`toepassingsbereik` alleen al op 12,5%) en de dubbele punt in
`Onderwerp: typeBepaling` als asscheider (30,4% van de artikelen, en rechts ervan
staat net zo vaak iets inhoudelijks).

> Correctie 2026-08-08: hier stond eerder "373.364 unieke opschriften". Dat
> telde over álle `bron_soort`-waarden in `v2a.tekst_embedding` en werd
> gedomineerd door Artikel-chunks met een vrijwel uniek laagste segment. Het
> zuivere getal is 18.283; de conclusie verandert niet.

Op **pad**-niveau is cureren wél tractabel — zie fase 1.

## 3. Doelontwerp — een opzoektabel, geen model

**Het opschriftpad is een bruikbare joinsleutel.** Gemeten 2026-08-09:
148.282 artikelen hebben 10.798 verschillende voorouderpaden; na normaliseren
(kleine letters, accenten, leestekens, nummering) blijven er **9.999** over.
Slechts 7% spellingsvarianten — het is geen matchingprobleem maar een
sleutelprobleem, en dat is opgelost met `lower()` plus wat `regexp_replace`.

Dekking van een kale opzoektabel op die genormaliseerde sleutel:

| gecureerde sleutels | artikelen | dekking |
|---:|---:|---:|
| 70 | 93.963 | **63,4%** |
| 300 | 106.029 | 71,5% |
| 1.000 | 116.598 | 78,6% |
| 2.000 | 124.759 | 84,1% |

Duizend handmatig gelabelde paden dekken bijna 79% — **zonder embeddings,
zonder clustering, zonder centroïdes**. Dat maakt de hele
discovery-machinerie van `build_categorie.py` overbodig voor deze as.

### De twee assen, elk als opzoeking

| as | sleutel | bron van de waarde |
|---|---|---|
| **categorie / subcategorie** | genormaliseerd voorouderpad | handcuratie (`categorieen-v2.xlsx`) |
| **typeBepaling** | genormaliseerd artikelopschrift | gesloten lexicon van ~25 waarden |

De typeBepaling-as heeft **geen curatie nodig**: het lexicon uit fase 0 is er
al en is op 95,4% dekking gevalideerd. Die as kan dus vooruitlopen op de
handcuratie van de andere.

### Besluit: niet ingedeeld boven verkeerd ingedeeld

*(gebruiker, 2026-08-09)* Geen sleuteltreffer → **geen categorie**. Niet
raden, niet naar een embedding-terugval grijpen, niet naar de dichtstbijzijnde
buur schuiven.

Gevolg voor de UI: bij 1.000 gecureerde paden heeft ~21% van de artikelen geen
categorie. Dat wordt getoond als een grijze knop **"niet ingedeeld (n)"** naast
de categorieknoppen — zichtbaar en telbaar, niet weggemoffeld. Vandaag krijgt
100% een categorie en is een onbekend deel daarvan fout; dat is de slechtere
ruil.

De embedding-terugval voor de staart is daarmee **uit scope**. Hij kan later
alsnog, en is dan meteen meetbaar: het handgecureerde deel is zijn ijkpunt.

### Datamodel

```sql
CREATE TABLE v2a.pad_categorie (
    pad_sleutel   TEXT PRIMARY KEY,   -- genormaliseerd voorouderpad
    pad_voorbeeld TEXT,               -- één ruwe schrijfwijze, voor de mens
    categorie     TEXT,
    subcategorie  TEXT,
    n_artikelen   INT,
    n_bronhouders INT,
    bron          TEXT                -- 'curatie'
);

CREATE TABLE v2a.artikel_indeling (
    tekst_element_id    BIGINT PRIMARY KEY REFERENCES p2p.tekst_element(id) ON DELETE CASCADE,
    regeling_expression TEXT NOT NULL,
    wid                 TEXT,
    pad_sleutel         TEXT,
    categorie           TEXT,         -- NULL = niet ingedeeld
    subcategorie        TEXT,
    type_bepaling       TEXT,         -- NULL = niet herkend
    herkomst            TEXT          -- 'pad-curatie' | 'artikelopschrift' | 'beide'
);
```

`v2a.categorie` en `v2a.chunk_categorie` blijven bestaan zolang de
vector-retrieval ze gebruikt, maar zijn niet langer de bron voor de UI.

Drie regels blijven staan uit de eerdere versie van dit plan:

1. **Eén eenheid.** Indeling op artikel; leden erven. Het pad ís een
   artikeleigenschap, dus de 30,9% ruziënde leden kan niet meer ontstaan.
2. **Onthouden mag** — nu geen drempelparameter meer maar simpelweg: geen
   sleuteltreffer, geen waarde.
3. **Uitlegbaar tonen.** De UI toont het pad dat de indeling droeg.

## 4. Uitvoering

### Fase 0 — dekkingsmeting · UITGEVOERD 2026-08-08 · **GO**

Vraag: voor hoeveel eenheden levert de voorouderketen een **onderwerpdragend**
opschrift op? Niet "heeft dit een opschrift" (97,9%, zegt niets), maar: blijft
er na het wegstrepen van typeBepaling-woorden nog iets over dat een onderwerp noemt?

Methode: recursieve keten over `parent_id` tot de wortel, per segment de
nummering en de staart achter een dubbele punt strippen, dan toetsen tegen een
lexicon van ~70 typeBepaling-patronen (toepassingsbereik, begripsbepalingen,
normadressaat, meet- en rekenbepalingen, gegevens en bescheiden,
bestemmingsomschrijving, bouwregels, wijze van meten, …). Bewust ruim
afgesteld: liever een onderwerp per ongeluk als typeBepaling wegstrepen dan
andersom, zodat de uitkomst een **ondergrens** is.

| groep | eenheid | n | onderwerpdragend |
|---|---|---:|---:|
| Ow-artikelstructuur | Artikel | 148.282 | **98,5%** |
| Vrijetekst-instrumenten | Divisietekst | 34.688 | **97,5%** |
| Wro-plannen (IMROPT) | Artikel | 696.378 | **94,6%** |
| **totaal** | | **879.348** | **95,4%** |

Per documenttype (Ow-artikelen): Omgevingsplan 99,1% (126.480) ·
Omgevingsverordening 99,3% · Waterschapsverordening 99,0% · AMvB 99,5% ·
Voorbeschermingsregels Omgevingsplan 86,3% · Aanwijzingsbesluit N2000 48,3% ·
Toegangsbeperkingsbesluit 0% (96 artikelen, geen enkel opschrift).
Vrijetekst: Omgevingsvisie 98,8% · Programma 96,1% · Projectbesluit 97,4%.

**Drie conclusies.**

1. **Go.** De embedding-laag wordt terugvalpositie, niet hoofdmoot. Ook voor
   Wro, dat vooraf als risico gold: de IMROPT-keten
   `Regels > 2 Bestemmingsregels > Artikel 3 Wonen > 3.1 Bestemmingsomschrijving`
   draagt de bestemmingsnaam als onderwerp.
2. **De keten is echt nodig, niet alleen het naaste kopje.** Bij **115.139
   eenheden (13%)** is het diepste opschrift een typeBepaling-woord terwijl er
   hogerop wél een onderwerp staat. Dat is de directe empirische grond onder
   variant E boven variant B uit §2.
3. **Het gat is grotendeels géén gat.** Wat als "alleen typeBepaling" uitvalt zijn
   overwegend artikelen die inderdaad geen onderwerp hebben:
   `ALGEMENE BEPALINGEN > Begripsbepalingen`, `SLOTBEPALINGEN > (citeertitel)`,
   `Overgangsrecht > Toepassingsbereik`, en aan Wro-kant de 37.641
   Begrippen-artikelen. Daar is **onthouden het juiste antwoord**, geen
   tekortkoming. Echt problematisch is alleen wat helemaal geen opschrift heeft:
   363 Ow-artikelen (waarvan 96 Toegangsbeperkingsbesluit en 255 N2000) en 315
   Divisieteksten.

**Caveat**: dit meet dekking, niet kwaliteit. Het typeBepaling-lexicon is
handgemaakt; een kopje kan onderwerpdragend heten en toch nietszeggend zijn
("Overige regels"). Het kwaliteitsbewijs is de A/B in §2, niet deze meting.

Script: `scratchpad/f0b.py` + `f0d.py` (meetscripts, geen productiecode).

### Fase 1 — tabellen en bouwscript · UITGEVOERD 2026-08-09

- `scripts/2026-08-add-pad-categorie.sql` — de twee tabellen hierboven.
- `scripts/bouw_indeling.py` — leest `curatie/categorieen-v2.xlsx`, vult
  `pad_categorie`, materialiseert `artikel_indeling` met de recursieve
  padquery, en leidt `type_bepaling` af uit het artikelopschrift.
  Idempotent: TRUNCATE + herbouw.

### Fase 2 — typeBepaling-as · UITGEVOERD 2026-08-09 · GEEN CURATIE NODIG

Gesloten lijst van ~25 waarden, afgeleid uit het fase-0-lexicon
(toepassingsbereik, oogmerk, begripsbepaling, normadressaat, zorgplicht,
maatwerkvoorschrift, meet- en rekenbepaling, gegevens en bescheiden,
meldingsplicht, vergunningplicht, aanvraagvereisten, beoordelingsregel,
overgangsrecht, voorrangsbepaling, slotbepaling, gereserveerd, plus de
Wro-vormen bestemmingsomschrijving / bouwregels / gebruiksregels /
afwijkingsregel / wijzigingsbevoegdheid / nadere eisen / aanlegregel /
wijze van meten / anti-dubbeltelregel).

Deze as kan meteen draaien en opleveren — hij wacht niet op de handcuratie.

**Resultaat op 148.282 artikelen**: 51.624 (**34,8%**) krijgen een
typeBepaling. Grootste waarden: toepassingsbereik 18.695 · gegevens en
bescheiden 9.537 · overgangsrecht 5.577 · meet- en rekenbepaling 4.720 ·
beoordelingsregel 3.192 · gereserveerd of vervallen 2.218 · oogmerk 1.599 ·
maatwerkvoorschrift 1.103 · vergunningplicht 1.056 · begripsbepaling 1.029.

**Vondst die het model bijstelt.** De 65,2% zonder typeBepaling zijn geen
mislukte matches — het zijn artikelen waarvan het opschrift een **onderwerp**
noemt in plaats van een type:

```
bodem: bodembeschermende voorziening       lozen van afvloeiend hemelwater
water: lozingsroute                        opstelplaatsen voor brandweervoertuigen
bodem: eindonderzoek bodem                 aansluiting op distributienet voor drinkwater
```

Het aanvankelijke model — *pad = onderwerp, artikelopschrift = typeBepaling* —
was te strak. Het artikelopschrift draagt **soms** een type en **soms** een
fijner onderwerp; welke van de twee is aan het opschrift zelf te zien, en dat
is precies wat de gesloten lijst doet. 34,8% is dus het eerlijke aandeel
artikelen dat werkelijk een bepalingstype in de kop zet, geen tekortkoming.

Praktisch gevolg: het typeBepaling-filter bedient ruim een derde van het
corpus, en juist het deel waar de vraag "laat me alle meldplichten zien"
zinnig is. De overige artikelopschriften zijn **onderwerp-materiaal** en
kunnen later de subcategorie-as verrijken (niet in scope nu).

### Fase 3 — handcuratie · ~1 uur van de gebruiker · **WACHT OP GEBRUIKER**

**Herzien 2026-08-09 na gebruikersfeedback.** De eerste opzet zette het
opschriftpad zélf als subcategorie neer, met namen als *"aanvraagvereisten
omgevingsvergunningen vereist op grond van een andere gemeentelijke regeling
dan dit omgevingsplan in samenhang met artikel 22.8 van de Omgevingswet"*.
Correct maar onleesbaar, en een filter met 1.151 knoppen is geen filter.

De fout was pad en label gelijkstellen. Het pad is **bewijs** — het bepaalt
betrouwbaar wélke artikelen bij elkaar horen — de naam is een aparte,
menselijke keuze uit een kleine lijst. De koppeling is veel-op-een:
**10.798 paden → ~200 labels**.

Voorstel-label per pad = het **kortste bruikbare kopje** in de keten. Niet het
diepste (dat is de volzin) en niet het ondiepste (dat is de boekdeel-container).
In de praktijk levert dat het kopje op dat de wetgever als onderwerpsnaam
bedoelde: `geur`, `afvalwaterbeheer`, `bodembeheer`, `trillingen`,
`energiebesparing`, `zwerfafval`, `traditioneel schieten`.

Gemeten dekking van die labellijst: top-25 = 68,4% · top-50 = 75,1% ·
top-100 = **80,1%** · top-200 = 84,8%. Dezelfde orde van grootte als de oude
productielijst (47 subcategorieën onder 21 thema's), dus dezelfde UI-maat.

**Waarom dit niet dezelfde fout maakt als de vorige curatie**: toen benoemde
een naam een cluster dat je niet kon inspecteren, en daarom kon "Tanken en
vloeibare brandstoffen" blijven zitten op een stapel toepassingsbereik-zinnen.
Nu benoemt de naam een groep die door het opschriftpad is gedefinieerd, en die
paden staan ernaast in het werkblad. Verkeerd labelen is zichtbaar.

**Werkblad-indeling** (`genereer_curatie_xlsx.py`):

| blad | rijen | wat je doet |
|---|---:|---|
| Labels | 200 | **het echte werk** — naam herschrijven, categorie kiezen, of samenvoegen met een ander label. Werkt door naar alle paden eronder. |
| Paden (uitzonderingen) | 1.500 | niet doorlopen; alleen overschrijven waar het automatische label aantoonbaar misgaat |
| Bestaande categorieen | 47 | de huidige subcategorieën: typeBepaling, onderwerp, samenvoegen of afkeuren |

Vlaggen in de kolom `let op` sturen de aandacht: **43** labels "te lang →
herschrijven" (>32 tekens, want een label wordt een filterknop), **7**
"structuur → niet indelen" (`bruidsschat`, `thema's`, `voormalige
rijksregels`), **1** "te grof → splitsen" (`milieubelastende activiteiten`,
26.456 artikelen). De overige **149** zijn bruikbaar zoals ze staan.

`curatie/categorieen-v2.xlsx` invullen. Blad *Paden*: rij 70 = 63,4% dekking,
rij 300 = 71,5%, rij 1.000 = 78,6%. Blad *Bestaande categorieen*: 47 regels,
kolom `beslissing`.

**Kolom `bevestigd`.** Het laadscript negeert elke rij zonder `x`. Reden: de
generator vult `subcategorie` altijd voor en `categorie` soms (uit
IMOW-annotaties). Zonder expliciet vinkje zou het script die voorinvulling niet
van een echt oordeel kunnen onderscheiden — en een machine-label voor een
mensen-label aanzien is precies waar dit hele traject mee begon. Bij de eerste
proefrun telde `bouw_indeling.py` 839 "gecureerde" paden die in werkelijkheid
allemaal voorinvulling waren; met de vinkkolom staat de teller nu terecht op 0.

### Fase 4 — API · UITGEVOERD 2026-08-09

`ocd-api/main.py`, endpoint `/v1/viewer/regeling/{expr}/onderwerpen` (rond
regel 4333): lezen uit `v2a.artikel_indeling` in plaats van
`chunk_categorie`, en twee assen teruggeven — `categorieen` en
`type_bepalingen` — plus `niet_ingedeeld` als expliciete telling.

De bestaande wId-zeef (rond regel 4361) blijft nodig: de indeling draait op het
werk, de weergave op de expressie.

### Fase 5 — viewer · UITGEVOERD 2026-08-09

`omgevingsdocumentenregister.nl/public/app.js`: tweede filterstrook, plus de
grijze "niet ingedeeld"-knop. De lid-naar-artikel-vouwing (`widLi`, rond regel
445) wordt overbodig — de API levert nu al op artikelniveau.

**Endpoint** `/v1/viewer/regeling/{expr}/onderwerpen` leest nu uit
`v2a.artikel_indeling` en geeft drie dingen terug: `categorieen` (met `sub`),
`type_bepalingen`, en `niet_ingedeeld` als eersterangs veld met eigen telling.
`onderwerpen` blijft als alias staan zodat een nog niet uitgerolde frontend niet
breekt.

Twee dingen konden weg. De **werk/expressie-kunstgreep** (joinen op het werk
omdat de vector-laag achterliep, met `split_part(…, '/nld@', 1)`) is niet meer
nodig: deze tabel hangt rechtstreeks aan `p2p.tekst_element`. En de **wId-zeef**
die corrigeerde voor elementen die intussen verdwenen waren evenmin — gemeten op
gm0358: 549 wId's uit het endpoint, 549 artikel-wId's in de boom, **0 wezen**.

**Viewer**: tweede knoppenrij "Soort bepaling" (streepjesrand, ander accent dan
de onderwerpknoppen — het zijn twee assen, geen twee niveaus), plus een grijze
gestippelde knop **"niet ingedeeld (n)"** achteraan de eerste rij.

`pasFilterToe` werkt nu met een **doorsnede over assen**: binnen een as is het
"of" (geur of geluid), tussen de assen "en". *Toepassingsbereik binnen geur* is
de vraag die iemand stelt; "alles wat geur is plús alles wat toepassingsbereik
is" niet. Een as zonder actieve knop legt geen beperking op.

De dekkingsregel onderaan noemt nu ook wat er níét is ingedeeld, en de zin
"de artikelsgewijze toelichting telt niet mee" is vervangen door "indeling op
basis van de kopjes boven het artikel" — dat is wat er nu werkelijk gebeurt.

Getoetst op gm0358 artikel 22.96: **milieu › geur**, typeBepaling
**toepassingsbereik**.

### Fase 6 — productie + verificatie · UITGEVOERD 2026-08-09

`2026-08-06-categorie-naar-productie.py` uitbreiden met de twee nieuwe
tabellen. Pariteitscontrole moet nu op `p2p.tekst_element.id` in plaats van
`v2a.tekst_embedding.id`. Daarna een handmatige controle op vijf documenten,
waaronder gm0358 artikel 22.96.

### Raming

**Ongeveer anderhalve dag**, plus een uur curatie van de gebruiker.

> Eerdere raming in dit document was drie werkdagen plus anderhalve week
> doorlooptijd. Die rekende met de oude pijplijn — discovery, HDBSCAN,
> hercuratie van centroïdes, 738.000 chunks opnieuw embedden — machinerie die
> dit ontwerp juist overbodig maakt. Bijgesteld 2026-08-09.

**Kopiëren op de natuurlijke sleutel, niet op id.** De pariteitscheck sloeg
alarm en had gelijk: lokaal en prod hebben allebei 154.725 artikelen maar een
verschillende md5 over de id-lijst — de serials zijn in een andere volgorde
uitgedeeld. Kopiëren op `tekst_element_id` zou de indeling aan de verkeerde
artikelen hebben gehangen. `indeling_naar_productie.py` gaat daarom over
`(regeling_expression, wid)`, aan beide kanten uniek voor alle 154.725
artikelen met nul ontbrekende wids, en zoekt de FK op prod opnieuw op.

Dat is een verschil met `2026-08-06-categorie-naar-productie.py`, dat wél op id
mag kopiëren omdat `v2a.tekst_embedding` daar aantoonbaar identiek is. Wie een
volgend script schrijft: meet het, neem het niet aan.

**Uitgerold**: 9.999 paden + 148.282 artikelen, 0 wezen bij het opzoeken.
Oude tabellen staan als `*_oud`; terugdraaien is twee renames.

**Live geverifieerd** op `omgevingsdocumentenregister.nl/api/…/onderwerpen`
voor gm0358: 549 artikelen, 505 ingedeeld, 44 niet, 229 met een typeBepaling.
Bouwen 224 · milieu 92 (geur 35) · water 37 · geluid 36. TypeBepalingen:
toepassingsbereik 76 · beoordelingsregel 49 · gegevens en bescheiden 25.

## 5. Wat we NIET doen

- **Embeddings of clustering voor deze indeling.** Een opzoektabel op 1.000
  sleutels haalt 78,6%; de rest krijgt "niet ingedeeld". De embedding-terugval
  is een latere, optionele verbetering met het gecureerde deel als ijkpunt.
- **`build_categorie.py` verbouwen.** Die blijft draaien voor de
  vector-retrieval; de UI hangt er niet meer aan.
- **De categorie `kandidaat.49` los afkeuren.** Dweilen — de clustering
  produceert per definitie opnieuw typeBepaling-clusters. Wel bruikbaar als
  tussentijdse pleister zolang fase 4/5 nog niet live zijn.
- **De artikeltekst meenemen in de categorie-bepaling.** Gemeten schadelijk
  (§2).
- **Diepteweging over het opschriftpad.** Gemeten schadelijk (−7,6 punt).
