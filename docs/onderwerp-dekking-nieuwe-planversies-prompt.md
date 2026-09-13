# Prompt — Waarom nieuwe planversies slechter ingedeeld raken (G-127)

> Plak dit als opdracht in een nieuwe sessie met toegang tot de OCD-repo.

---

## Taak

Zoek uit waarom de onderwerp-as (`v2a.artikel_indeling`) nieuw geladen
planversies structureel slechter indeelt dan het landelijk gemiddelde, stel
vast of dat een curatie-achterstand of een systematisch defect is, en lever
een onderbouwd voorstel plus de correcties die je kunt aantonen.

Dit is een **diagnose-opdracht**. Lever geen fix die je niet gemeten hebt.

## De meting die dit opriep

Gemeten 2026-08-13 na een sync met acht nieuwe planversies:

| | artikelen | met categorie | |
|---|---:|---:|---:|
| landelijk | 148.514 | 125.747 | 84,7% |
| de acht nieuwe versies | 2.216 | 1.577 | 71,2% |
| Epe | 498 | 247 | 49,6% |
| Ede | 316 | 163 | 51,6% |
| Stede Broec | 304 | 295 | 97,0% |

De nieuwe versies hebben *méér* artikelen dan hun voorgangers (2.216 tegen
1.984) en worden *slechter* ingedeeld. Stede Broec op 97,0% laat zien dat het
geen eigenschap van "nieuw" op zichzelf is.

## Wat het níét is — jaag hier niet op

1. **wId-drift.** Er bestaat een oudere bevinding dat de wId-overlap tussen de
   vectorlaag en `p2p` bij Ede exact nul was. Die route is dood: het endpoint
   `/v1/viewer/regeling/{expr}/onderwerpen`
   ([main.py:4449](../ocd-api/main.py#L4449)) leest sinds 2026-08 rechtstreeks
   uit `v2a.artikel_indeling` op `regeling_expression`, zonder wId-zeef en
   zonder werk/expressie-kunstgreep. Verifieer dat en laat het dan los.
2. **De `−61 ingedeelde artikelen` uit het sync-rapport.** Dat verschil
   vergelijkt de eerste `indeling`-run in `core.load_run` met een handmatig
   opgebouwde stand van onbekende datum. Als regressiesignaal waardeloos.
3. **Embeddings.** `bouw_indeling.py` gebruikt ze niet. De indeling is een
   opzoeking van het genormaliseerde voorouderpad tegen een handgecureerde
   lijst. Zoek de oorzaak in de sleutel, niet in een model.

## Context

- DB: Postgres in Docker-container `dso-postgis` (lokaal). Verbind via
  `from src.db import get_conn` vanuit `c:/GIT/OCD/dso-loader` (leest `.env`).
- Bouwscript: [`dso-loader/scripts/bouw_indeling.py`](../dso-loader/scripts/bouw_indeling.py).
  Twee assen: `categorie`/`subcategorie` uit het **voorouderpad** tegen
  `curatie/lijsten.xlsx`, en `type_bepaling` uit het **artikelopschrift** tegen
  een gesloten lexicon in het script zelf. Alleen de eerste as heeft curatie
  nodig.
- Tabellen: `v2a.pad_categorie` (pad_sleutel → categorie, met `n_artikelen`,
  `n_bronhouders`), `v2a.artikel_indeling` (per `tekst_element_id`, met
  `pad_sleutel`, `categorie`, `type_bepaling`, `herkomst`).
- Ontwerp: [`docs/onderwerp-as-en-typebepaling-as.md`](onderwerp-as-en-typebepaling-as.md).
- **Harde regel uit dat ontwerp** (gebruikersbesluit 2026-08-09): `NULL` is een
  geldig antwoord. Niet raden, niet naar de dichtstbijzijnde buur schuiven.
  Een terugvalroute mag je *voorstellen* en meten, maar niet zomaar inbouwen.

## Hypothesen, in volgorde van hoe interessant ze zijn

### H1 — De curatie is op de bruidsschat gebouwd en straft eigen regels af

Dit is de belangrijkste en moet als eerste getoetst. De curatie is opgebouwd
uit de grootste paden: 10.798 ruwe paden vallen na normaliseren samen tot
9.999, en de 1.000 grootste dekken 78,6% van alle artikelen. Maar de grootste
paden zijn per constructie de **bruidsschat**, die woordelijk identiek in ~340
gemeenten staat. Eigen regels van een gemeente zitten dus per definitie in de
staart.

Als dat klopt, dan geldt: *hoe meer een gemeente zélf regelt, hoe slechter haar
dekking* — en dan meet de onderwerp-as vooral hoe bruidsschat-achtig een plan
is. Dat zou een dekkingskaart precies verkeerd om kleuren.

**Toets**: bepaal per bronhouder het aandeel niet-bruidsschat-artikelen (dedup
op kale tekst: artikelen waarvan de tekst bij ≥N andere bronhouders identiek
voorkomt zijn bruidsschat) en zet dat af tegen het percentage ingedeeld.
Verwacht bij H1 een duidelijk negatief verband. Rapporteer het verband, niet
alleen een correlatiegetal — laat de tien uitersten aan beide kanten zien.

### H2 — Normalisatie-misser: het pad bestaat wél, maar valt net anders

De sleutel is een genormaliseerd voorouderpad. Kandidaat-oorzaken: afwijkende
nummering in het opschrift, leestekens, niet-brekende spaties, casing,
enkelvoud/meervoud, of een verschil in *diepte* van de boom.

**Toets**: neem de niet-ingedeelde paden van Epe en Ede en zoek per pad de
dichtstbijzijnde gecureerde `pad_sleutel` (Levenshtein of trigram via
`pg_trgm`). Een grote groep met kleine afstand betekent normalisatie-schuld en
is goedkoop te repareren. Een grote groep met grote afstand betekent H1 of H3.

### H3 — De boomstructuur van de nieuwe versie is anders

Een nieuwe planversie kan een tussenlaag toevoegen of hoofdstukken hernoemen
zonder dat het onderwerp verandert. Het pad wijzigt dan wél.

**Toets**: vergelijk voor dezelfde bronhouder de vorige en de nieuwe expressie.
Welke `pad_sleutel`-waarden verdwijnen, welke verschijnen, en zijn de nieuwe
herkenbaar als een variant van de oude?

### H4 — Er is helemaal geen pad

Artikelen zonder opschriftpad hebben niets om op te zoeken.

**Toets**: tel bij de niet-ingedeelde artikelen hoeveel er een leeg of
één-niveau-diep pad hebben. Als dat groot is, is het geen curatie-probleem maar
een extractie-probleem in de padopbouw.

### H5 — Selectie-artefact

Acht versies is weinig. Stede Broec haalt 97,0%.

**Toets**: draai dezelfde meting over álle regelingen, gegroepeerd op
laaddatum of expressiedatum. Is "nieuwer = slechter" een trend of zijn Epe en
Ede twee toevalstreffers?

## Methode

1. Reproduceer eerst de cijfers uit de tabel hierboven. Wijken ze af, meld dat
   en zoek uit waarom vóór je verder gaat.
2. Toets H1 tot H5 in die volgorde, maar stop niet bij de eerste bevestiging —
   ze sluiten elkaar niet uit en het mengsel is het antwoord.
3. Kwantificeer elke hypothese: hoeveel van de 639 niet-ingedeelde artikelen in
   de acht nieuwe versies verklaart hij?
4. Alles wat je meet: leg de query erbij, zodat het herhaalbaar is.

## Op te leveren

1. **Rapport** in `docs/` met per hypothese de meting, het aandeel dat hij
   verklaart, en de conclusie.
2. **Correcties die je kunt aantonen** — bijvoorbeeld normalisatie-fixes uit H2
   — als patch op `bouw_indeling.py`, met het effect op de dekking vóór en na,
   gemeten op dezelfde stand.
3. **Voorstel over de terugvalroute**. De vraag uit G-127 is niet "de curatie
   bijwerken" (dat is dweilen) maar of onbekende paden een terugval verdienen,
   en zo ja welke. Toets een voorstel tegen het ijkpunt uit
   `docs/onderwerp-as-en-typebepaling-as.md` §2 (`p2p.juridische_regel.thema`,
   menselijke IMOW-annotatie, leden met precies één thema) zodat de
   nauwkeurigheid vergelijkbaar is met de 82,7% van variant E. Een terugval die
   onder de curatie scoort is geen terugval maar ruis.
4. **Voorstel voor sync-rapportage**: per run melden hoeveel van de *nieuw
   geladen* artikelen ingedeeld raakten, in plaats van één landelijk saldo.
   Dat is het signaal dat hier ontbrak.

## Randvoorwaarden

- Niet naar productie schrijven. Alle metingen en fixes lokaal; replicatie is
  een aparte beslissing.
- `NULL` blijft een geldig antwoord. Een terugvalroute die gaten dichtsmeert
  met een gok is een verslechtering, ook als het percentage stijgt.
- Werk de bevinding bij in `gaps.md` van de vault
  (`c:/GIT/OmgevingswetKnowledgeBase/vault_v1/gaps.md`, G-127) volgens de
  conventies in `CLAUDE.md` van die vault.
- Deze diagnose voedt de dekkingsmatrix uit
  [`docs/dekkingsmatrix.md`](dekkingsmatrix.md) §3.3: zolang dit open staat is
  `reden_leeg = 'pipeline'` voor een deel van de gevallen het enige eerlijke
  antwoord.
