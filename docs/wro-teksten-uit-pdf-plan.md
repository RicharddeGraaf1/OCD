# Wro-teksten uit pdf: oude plannen zonder tekst in de IHR

> **Scope**: de regels van ruimtelijke plannen waarvoor de IHR geen teksten levert, uit de pdf
> van de voorschriften halen, met tekstherkenning waar nodig. Ze komen in een eigen tabel naast
> `wro.wro_tekst_object_v2`, met hun herkomst erbij.
> **NOT covered**: de toelichting en de plankaart-pdf, het indelen in onderwerpen (dat doet
> `bouw_wro_indeling.py` daarna), en de transitie-export die deze teksten gebruikt
> (`docs/transitie-export-plan.md`).
> **Status**: **Concept** (Richard, 2026-11-12). De proef op twee Haarlemse plannen is gedaan; de
> uitkomst staat bij [Proef](#proef-2026-11-12).

## Aanleiding

De transitiemodule van Locus Legis, de viewer en Regels op maat tonen de regels uit het tijdelijk
deel. Een groot deel van de oude plannen heeft in OCD geen enkele tekst. Gemeten op de lokale DB
(2026-11-12), actieve plannen zonder tekst:

| Soort | Actief | Zonder tekst | Bronhouders |
|---|---|---|---|
| gemeentelijk plan; bestemmingsplan artikel 10 (WRO 1965) | 2.961 | 2.961 | 303 |
| bestemmingsplan | 50.391 | 9.996 | 393 |
| wijzigingsplan | 8.159 | 1.640 | 215 |
| inpassingsplan | 422 | 114 | 12 |

Bij de bestemmingsplannen tellen oudere versies binnen een dossier mee, dus het aantal dat nog
geldt, ligt lager. Dat telt stap 1.

Ook deze plannen horen bij het tijdelijk deel van het omgevingsplan. Voor de artikel 10-plannen
schrijft IPLO: *"Ook onder de oude Wet op de Ruimtelijke Ordening (WRO) vastgestelde
bestemmingsplannen worden onderdeel van het tijdelijke deel"* (vault G-181).

De IHR (opvragen v4) markeert deze plannen met `isHistorisch: true`. `/teksten` is leeg. Wat er
wel is, staat in `heeftOnderdelen`: drie pdf's op ruimtelijkeplannen.nl, met het voorvoegsel `p_`
(plankaart), `v_` (voorschriften/regels) en `t_` (toelichting).

## Proef (2026-11-12)

Twee Haarlemse artikel 10-plannen. De `v_`-pdf, Tesseract 5.4 met het Nederlandse model
(`tessdata_best/nld`), zes pagina's tegelijk:

| | De Appelaar 2000 (`NL.IMRO.03920000108bp005-`) | Oude Stad (`NL.IMRO.03920000100bp001-`) |
|---|---|---|
| Pagina's, waarvan voorschriften | 55, waarvan 21 | 238, waarvan 193 |
| Tekstlaag in de pdf | geen (scan, 1-bit tiff) | geen |
| OCR-tijd | 12 s | 42 s |
| Gemiddelde woordzekerheid | 94,5% | 90,1% (laagste pagina 29,4%) |
| Artikelen herkend | 16 | 79 |

Wat de proef laat zien:

1. **De herkenning is goed en snel** op getypte scans: ongeveer 0,2 seconde per pagina. Pagina 7
   van De Appelaar komt overeen met de scan, op één aanhalingsteken na. Landelijk gaat het om
   honderdduizenden pagina's, en dat is haalbaar in enkele nachten.
2. **De pdf is een bundel.** Na de voorschriften volgen procedurestukken: het advies van de
   Provinciale Planologische Commissie, de raadsnota en het raadsbesluit. Die moeten eruit, anders
   wordt een zin als "Artikel 5 van de voorschriften" een artikelkop.
3. **Wat geldt, staat niet in de voorschriften alleen.** Bij De Appelaar adviseert de PPC om
   *"goedkeuring te onthouden aan artikel 5 van de voorschriften"* (p. 25). Onder de WRO van 1965
   gold een voorschrift waaraan GS goedkeuring onthielden niet. Dat is mijn lezing van de
   goedkeuringsregeling (artikel 28 WRO); ⚠️ te verifiëren. Het besluit van GS zelf zit niet in
   de pdf. Of artikel 5 geldt, is uit deze bundel dus niet af te lezen. Oude Stad heeft
   vergelijkbare passages (p. 191, 236) en een artikel 39 met de kop "VERVALLEN".
4. **De opbouw is het lastige deel.** De inhoudsopgave beslaat soms meer dan één pagina. "HOOFDSTUK
   II" wordt gelezen als "IL". Genummerde subonderdelen lijken op leden, wat dubbele leden oplevert.
   Tabellen per bouwblok (Oude Stad) worden platte tekst.

Proefscript en uitvoer staan buiten de repo (scratchpad van de sessie). Stap 2 maakt er een loader
van.

## Ontwerp

**Tabel `wro.wro_tekst_uit_pdf`**, met dezelfde kolommen als `wro_tekst_object_v2` (`instrument_idn`,
`identificatie`, `volgnummer`, `niveau`, `parent_id`, `object_type`, `nummer`, `naam`, `inhoud`). Zo
werken de indeling, de tegels van H en de export erop zonder tweede codepad. Daarnaast:

| Kolom | Inhoud |
|---|---|
| `bron_url` | de `v_`-pdf |
| `pagina_van`, `pagina_tot` | waar de tekst in de pdf staat, voor de link vanuit Locus en de viewer |
| `methode` | `tekstlaag` of `ocr` |
| `ocr_zekerheid` | gemiddelde woordzekerheid van de tekst |
| `herkenning_versie` | Tesseract-versie, model en versie van de opbouwregels |
| `gecontroleerd_door`, `gecontroleerd_op` | leeg tot iemand de tekst tegen de pdf heeft gelezen |

**Tabel `wro.wro_pdf_bundel`**, per plan: de url, het aantal pagina's, per pagina de soort
(`inhoudsopgave`, `voorschriften`, `procedure`, `bijlage`) en de **signalen**. Een signaal is een
passage die raakt aan wat er geldt: "goedkeuring onthouden", "VERVALLEN", "herziening".

**View `wro.wro_tekst_alle`**: `wro_tekst_object_v2` plus `wro_tekst_uit_pdf`, met een kolom `bron`
(`ihr` of `pdf`). Afnemers lezen de view. De tekst uit de IHR wint: een plan dat in v2 tekst heeft,
krijgt geen pdf-tekst.

**Waarom apart en niet in v2.** `laad_wro_teksten_v2.py` laadt v2 opnieuw uit de IHR. Afgeleide tekst
mag daarbij niet verdwijnen, en hij moet altijd herkenbaar blijven als afgeleid.

**Identificatie op artikelnummer, niet op hoofdstuk.** De vorm is `pdf.art{nummer}` en
`pdf.art{nummer}.lid{nummer}`. Artikelnummers zijn in de oude plannen doorlopend, en een hoofdstuk
leest de OCR soms verkeerd. Zo blijft een verwijzing uit Locus naar hetzelfde artikel wijzen als de
herkenning opnieuw draait. Komt een nummer twee keer voor, dan krijgt het plan een signaal en geen
stille tweede rij.

**Juridische status.** Afgeleide tekst is niet authentiek. Elke afnemer toont hem als "uit de pdf,
controleer", met een link naar de pagina. Locus laat een artikel uit de pdf pas in een
voorrangsregel toe als het gecontroleerd is (plan 4 van de transitie-epic). Een plan met een
signaal "goedkeuring onthouden" krijgt die melding overal waar het verschijnt.

## Stappen

0. **Nauwkeurigheid meten, vóór alles.** De 90 tot 95% uit de proef is de eigen zekerheid van
   Tesseract, gemiddeld per woord, en geen gemeten foutpercentage. Voor plantekst is elke fout in
   een cijfer of een ontkenning er een te veel. Lees tien voorschriftenpagina's uit verschillende
   plannen en jaren met de hand. Meet daartegen per route:
   - het aandeel tekens dat fout is;
   - het aandeel artikel- en lidnummers dat fout is;
   - het aantal fouten dat de betekenis verandert ("niet", cijfers, maten).

   Routes om te vergelijken: Tesseract zoals in de proef, Tesseract met voorbewerkte beelden, en een
   nauwkeuriger route voor alleen de voorschriftenpagina's. De keuze en de verplichte controle volgen
   uit de meting, en worden hier vastgelegd.
1. **Omvang.** Per gemeente het aantal **geldende** plannen zonder tekst: per dossier de nieuwste
   versie. Daarnaast per soort het aandeel `v_`-pdf's met een tekstlaag, op een steekproef van 50.
2. **Loader** `src/loaders/wro_pdf.py`:
   - de pdf ophalen;
   - de tekstlaag lezen waar die er is, anders OCR per pagina;
   - de pagina's indelen naar soort;
   - de opbouw herkennen (hoofdstuk, artikel, lid);
   - de signalen zoeken;
   - schrijven in één transactie per plan.

   CLI: `wro-pdf plan <idn>` en `wro-pdf gemeente <gm-code>`. Tesseract is een vereiste; de loader
   meldt het en stopt als die ontbreekt.
3. **Opbouwregels verbeteren** op Haarlem (56 plannen) en Nunspeet (7). Doel: geen dubbele
   identificaties, inhoudsopgave en procedurestukken eruit, Romeinse cijfers hersteld. De gevallen
   die overblijven, gaan als signaal mee.
4. **View en indeling**: `wro_tekst_alle`, en `bouw_wro_indeling.py` laten lezen uit de view. Let op:
   een pdf-plan heeft geen planobjecten, dus de route via een bestemming valt weg. Indelen gaat dan
   alleen via de kopjes.
5. **Naar productie** over de natuurlijke sleutel `(instrument_idn, identificatie)`, zoals de andere
   overzetscripts. Het downloaden in bulk alleen in het voorkeursvenster (22:00–06:00), na elkaar.
6. **Steekproef met de hand**: tien artikelen per gemeente tegen de pdf gelezen. Leg het aandeel
   vast dat woordelijk klopt, en het aandeel dat op de goede plek in de opbouw staat.

## Verificatie

- [ ] Haarlem: voor de 56 artikel 10-plannen staan er artikelen in `wro_tekst_uit_pdf`, of een
      signaal dat zegt waarom niet.
- [ ] Geen dubbele `(instrument_idn, identificatie)`.
- [ ] De Appelaar 2000 heeft het signaal "goedkeuring onthouden" bij artikel 5, met de pagina.
- [ ] Een tweede run over hetzelfde plan levert dezelfde identificaties.
- [ ] `laad_wro_teksten_v2.py` laat `wro_tekst_uit_pdf` ongemoeid.
- [ ] Steekproef: per gemeente tien artikelen; het aandeel woordelijk juist staat in dit document.

## Open vragen

1. **Het besluit van GS.** Waar staat het goedkeuringsbesluit van een artikel 10-plan, als het niet
   in de bundel zit? En is een plan met onthouden goedkeuring in de IHR als zodanig te herkennen?
   Tot dat duidelijk is, blijft het een signaal en geen gevolgtrekking.
2. **Tabellen.** Oude Stad heeft een bouwblokpagina per blok, met tabellen. Platte tekst volstaat
   voor tellen en indelen, maar niet om er een voorstel van te maken. Is daar later een aparte
   route voor nodig?
3. **Waar draait de OCR.** Lokaal, met het resultaat over de natuurlijke sleutel naar productie, of
   ook op apps-1? Voorstel: lokaal. Het is eenmalig werk per plan, en de server hoeft er geen
   Tesseract voor te krijgen.
