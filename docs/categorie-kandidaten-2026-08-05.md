# Categorie-taxonomie v1-2026-07-07 — kandidaten ter beoordeling

Opgesteld 2026-08-05 uit `v2a.categorie`. Bron van de opbouw:
`OCD/dso-loader/scripts/build_categorie.py`.

## Lees dit eerst: het zijn twee verschillende dingen

**De ruggengraat** (21 stuks, status `bevestigd`, bron `imow-thema`) is de
lijst niet-deprecated IMOW-thema's uit `core.imow_thema`. **Alle 737.911
chunk-toewijzingen gaan hierheen** — stap 6 wijst uitsluitend toe aan de
ruggengraat.

**De kandidaten** (74 stuks, bron `discovery`) komen uit HDBSCAN op
gededupliceerde chunk-embeddings. Hun naam is de **top-3 TF-IDF-termen** van
het cluster — machinaal, niet door een mens gekozen. `n` is de clustergrootte
uit de discovery-run, **niet** een aantal toegewezen chunks: kandidaten hebben
er nul. Het zijn voorstellen tot uitbreiding, geen werkende classificatie.

Een kandidaat hangt onder het IMOW-thema waar zijn centroïde het dichtst bij
lag (cosine ≥ 0,60). Lag hij van álles ver af, dan staat hij los — die groep
staat bovenaan, want daar zit per definitie wat de IMOW-thema's missen.

---

## De ruggengraat, met werkelijke toewijzing

| IMOW-thema | toegewezen chunks | n bij opbouw |
|---|---:|---:|
| bodem | 169227 | 549 |
| geluid | 157144 | 103 |
| procedures | 149960 | 248 |
| bouwen | 78118 | 329 |
| natuur | 67592 | 367 |
| water | 41185 | 708 |
| landschap | 32403 | 70 |
| planologisch gebruik | 27769 | 282 |
| infrastructuur | 14508 | 316 |
| energie | 2 | 0 |
| mobiliteit | 2 | 0 |
| economie | 1 | 0 |
| erfgoed | 0 | 0 |
| gezondheid | 0 | 9 |
| landbouw | 0 | 0 |
| lucht | 0 | 9 |
| milieu | 0 | 0 |
| duurzaamheid | 0 | 0 |
| recreatie | 0 | 0 |
| veiligheid | 0 | 0 |
| wonen | 0 | 0 |

De nullen onderaan zijn IMOW-thema's die het corpus nauwelijks gebruikt.
Ze zijn niet 'fout' en ook niet zomaar te schrappen: ze komen uit de
officiële IMOW-waardelijst, dus een `DELETE` wordt bij de volgende
`build_categorie.py`-run gewoon teruggezet.

---

## Kandidaten per thema


### bodem

| n | voorgestelde categorie | status |
|---:|---|---|
| 190 | grotere diepte / plaatsvindt grotere / onttrekking plaatsvindt | kandidaat |
| 115 | beheer afvalwater / geloosd vuilwaterriool / afvalwater lozen | kandidaat |
| 114 | watermeter / onttrekken grondwater / grondwater onttrokken | kandidaat |
| 111 | verboden milieubelastende / verrichten zonder / melden verboden | kandidaat |
| 102 | sikb / ondergrondse opslagtank / erkenning bodemkwaliteit | kandidaat |
| 74 | Bodembescherming (o.a. schietbanen) | kandidaat |
| 68 | houdt specifieke / afmeerpaal / verholen | kandidaat |
| 45 | boorput / geschikt onttrekken / grondwater uitwisseling | kandidaat |
| 45 | dieper meter / boringsvrije / boringsvrije zone | kandidaat |
| 41 | Bodemkwaliteit bij bouwen | kandidaat |

### bouwen

| n | voorgestelde categorie | status |
|---:|---|---|
| 282 | octaafband / waarneempunt / geluidvermogen | kandidaat |
| 204 | subbrandcompartiment / vluchtroute / bouwconstructie | kandidaat |
| 144 | Bijbehorende bouwwerken en bouwhoogte | kandidaat |
| 118 | niets nodig / nodig voldaan / bouwwerk zonder | kandidaat |
| 60 | vloer / verbrandingslucht / rookgas | kandidaat |
| 59 | meter bouwhoogte / maximaal meter / bouwhoogte | kandidaat |
| 49 | regels aangewezen / gebruiksfunctie leden / voldaan naleving | kandidaat |
| 42 | goothoogte / alleen verleend / hart | kandidaat |

### economie

| n | voorgestelde categorie | status |
|---:|---|---|
| 228 | Circulaire economie en toekomstvisie | kandidaat |

### energie

| n | voorgestelde categorie | status |
|---:|---|---|
| 90 | Energietransitie en klimaat | kandidaat |
| 46 | Slagschaduw van windturbines | kandidaat |

### erfgoed

| n | voorgestelde categorie | status |
|---:|---|---|
| 220 | Aanvraagvereisten monumenten en archeologie | kandidaat |
| 42 | Monumenten (voorbescherming) | kandidaat |

### geluid

| n | voorgestelde categorie | status |
|---:|---|---|
| 151 | Geluidhinder van activiteiten | kandidaat |
| 140 | Actieplan geluid (beleid) | kandidaat |
| 111 | voldaan toepassing / toepassing geven / gemeenteweg | kandidaat |
| 110 | Geluidbeperkende maatregelen en plafonds | kandidaat |

### infrastructuur

| n | voorgestelde categorie | status |
|---:|---|---|
| 59 | afdeling gegevens / gezag afdeling / verricht verwachte | kandidaat |
| 49 | beperkt kwetsbare / kwetsbare kwetsbare / kwetsbare gebouwen | kandidaat |
| 40 | oordeel bevoegd / nevenfunctie / oordeel | kandidaat |

### landbouw

| n | voorgestelde categorie | status |
|---:|---|---|
| 50 | Veehouderij en geuremissie | kandidaat |

### landschap

| n | voorgestelde categorie | status |
|---:|---|---|
| 173 | aantasting kernkwaliteit / interpretatie toetsing / openheid | kandidaat |
| 79 | invulling behoefte / definitief aangewezen / voorkeursalternatief | kandidaat |

### milieu

| n | voorgestelde categorie | status |
|---:|---|---|
| 211 | Milieubelastende activiteiten (algemeen) | kandidaat |
| 97 | Geur van mest- en agrarische opslag | kandidaat |
| 52 | Tanken en vloeibare brandstoffen | kandidaat |

### natuur

| n | voorgestelde categorie | status |
|---:|---|---|
| 463 | Natuurwaarden en kernkwaliteiten | kandidaat |
| 393 | faunabeheereenheid / faunabeheerplan / wildbeheereenheid | kandidaat |
| 126 | ospar / deel noordzee / nederlandse deel | kandidaat |
| 123 | hectare eigendom / naam gebied / regio natuurbeheerplan | kandidaat |

### planologisch gebruik

| n | voorgestelde categorie | status |
|---:|---|---|
| 191 | gedragscode soortenbescherming / gedragscode / aardgasequivalent | kandidaat |
| 190 | emissie lucht / deelmetingen / totaal stof | kandidaat |
| 112 | uurgemiddelde / uurgemiddelde concentraties / meten concentratie | kandidaat |
| 74 | metalen paragraaf / tanken paragraaf / verricht voldaan | kandidaat |
| 52 | Toegestaan gebruik van gronden | kandidaat |

### procedures

| n | voorgestelde categorie | status |
|---:|---|---|
| 135 | Vergunningvoorschriften | kandidaat |
| 113 | Maatwerkvoorschriften | kandidaat |
| 107 | leefomgeving vastgelegd / begrensd bijlage / aangewezen geometrisch | kandidaat |
| 101 | Meldings- en gegevensverstrekking | kandidaat |
| 91 | opstelling zonne / opstellingen zonne / zonne energie | kandidaat |
| 73 | faalkans / overstromings / overstromings faalkans | kandidaat |
| 69 | milieueffectrapport / plan programma / aanzienlijke milieueffecten | kandidaat |
| 63 | Meet- en accreditatievoorschriften | kandidaat |
| 63 | omgevingsvergunning aanvraag / meervoudige aanvraag / meer activiteiten | kandidaat |
| 59 | Afwijkende waarden bij aangevraagde vergunning | kandidaat |
| 57 | artikelen theatergebruik / theatergebruik / bufferbewaarplaats | kandidaat |
| 50 | Verbods- en meldingsplicht activiteiten | kandidaat |
| 44 | inspecteur / selecteer / inspecteur vaststellen | kandidaat |
| 43 | inwerkingtreding verordening / activiteit inwerkingtreding / melding activiteit | kandidaat |

### water

| n | voorgestelde categorie | status |
|---:|---|---|
| 98549 | Lozen van afvalwater, grondwater en hemelwater | kandidaat |
| 16066 | Activiteiten bij waterkeringen en waterstaatswerken | kandidaat |
| 279 | Regionale waterkeringen en bergingscapaciteit | kandidaat |
| 277 | Activiteiten in beschermingszones waterkering | afgekeurd |
| 220 | Lozen op oppervlaktewater | afgekeurd |
| 201 | plaatsvindt beperkingengebied / beperkingengebied regionale / waterkering beperkingengebied | kandidaat |
| 175 | hoeveelheid natuurvriendelijke / verloren hoeveelheid / oever plaatsvindt | kandidaat |
| 102 | aanvangs / geplande aanvangs / aanvangs einddatum | kandidaat |
| 89 | Melding lozingsactiviteiten | kandidaat |
| 81 | behouden halen / halen gebied / gebied kernzone | kandidaat |
| 79 | erosiebestendige / voorschriften gelden / aansluit bestaande | kandidaat |
| 49 | begrenzing beperkingengebied / verordening geometrische / informatieobject | kandidaat |
| 49 | Verwijderen van waterwerken | afgekeurd |
| 48 | duiker / damwand / vlonder | kandidaat |
| 46 | voldaan specifieke / hoogheemraadschap voldaan / zorgplicht artikelen | kandidaat |
| 46 | omgevingsvergunning grondwater / grondwater onttrekken / onttrekkingsfilter | kandidaat |
| 41 | mede verstaan / uitvoeren graafwerkzaamheden / verstaan verplaatsen | kandidaat |
| 41 | Activiteiten bij waterkeringen | afgekeurd |
| 40 | leiding zone / aanleggen kabel / mantelbuis | kandidaat |

---

Totaal 78 voorstellen: 74 kandidaat, 4 al afgekeurd.