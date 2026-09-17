# Objectroutes in de indeling — uitvoeringsplan

**Status**: voorstel, nog niet gebouwd · **Datum**: 2026-09-17
**Onderbouwing**: vault-analyse `Objectroutes naast de padroute in de
categorie-indeling` (OmgevingswetKnowledgeBase), meting van 2026-09-16
**Meetscript**: `dso-loader/scripts/meet_indelingsroutes.py`
**Raakt**: `dso-loader/scripts/bouw_indeling.py`,
`dso-loader/scripts/indeling_naar_productie.py`, `v2a.artikel_indeling`

---

## 1. Wat dit plan wel en niet is

De vraag was of de indeling **objectgebaseerd** moet worden: de gebiedsaanwijzing
en de IMRO-bestemming als niveau 1 en 2, en die koppelen aan de teksten. De
meting zegt: niet als eigen indeling, wel als **bewijsroute naast het pad**, met
een volgorde die per documenttype verschilt.

Dit plan bouwt dus **geen tweede taxonomie**. Het houdt één indeling
(`v2a.artikel_indeling`, dezelfde categorieën) en breidt de manier waarop een
artikel aan zijn categorie komt uit van één route naar vier.

Wat nadrukkelijk niet doorgaat:

- `GebiedsaanwijzingType` als niveau 1 — mengt juridische typen (`functie`,
  `beperkingengebied`, `ruimtelijk gebruik`) met onderwerpen;
- de activiteitengroep als niveau-1-signaal — 71,7% dekking maar 42%
  overeenstemming, omdat `milieubelastende activiteit` alles op *milieu* zet;
- de gebiedsaanwijzing als primaire route — 2,6% van de Ow-artikelen.

## 2. De cijfers waar het plan op staat

Lokale OCD-DB, 2026-09-16. Volledige tabellen in de vault-analyse.

| corpus | eenheid | n | padroute | objectbewijs |
|---|---|---:|---:|---:|
| Ow-artikelstructuur | artikel | 149.775 | 84,2% | 77,1% |
| — omgevingsplan, landelijk pad | | 90.194 | 96,7% | 82,7% |
| — AMvB / verordening / MR | | 8.951 | 48–50% | **86–99%** |
| — N2000 / toegangsbeperking | | 589 | **0%** | **100%** |
| Wro (IMRO) | artikel | 696.378 | n.v.t. | 18,9% via naam |
| Vrijetekst | divisietekst | 31.145 | n.v.t. | 29,5% |

De kern in één zin: **het pad is sterk waar het objectbewijs zwak is, en
omgekeerd.** Daarom is de volgorde per documenttype de eigenlijke ingreep.

## 3. Ontwerp

### 3.1 De kruistabel (nieuw)

```sql
CREATE TABLE core.waardelijst_thema (
    waardelijst TEXT NOT NULL,   -- typegebiedsaanwijzing | gebiedsaanwijzinggroep
                                 -- activiteitgroep | bestemmingshoofdgroep
                                 -- dubbelbestemmingshoofdgroep | gebiedsaanduidinghoofdgroep
    waarde      TEXT NOT NULL,   -- kleine letters, zoals in p2p/wro opgeslagen
    thema       TEXT NULL REFERENCES core.imow_thema(label),  -- NULL = draagt geen onderwerp
    subcategorie TEXT NULL,      -- niveau 2; NULL = alleen niveau 1
    bron        TEXT NOT NULL,   -- imow-waardelijst | svbp | curatie
    PRIMARY KEY (waardelijst, waarde)
);
```

Vulling:

1. **IMOW-waardelijsten** — de thema-indeling zit al in de standaard; de
   groepering staat in de vault-analyse `IMOW Thema-categorisatie waardelijsten`
   en is als dictionary aanwezig in `meet_indelingsroutes.py`
   (`TYPE2THEMA`, `ACT2THEMA`). Die verhuist naar deze tabel.
2. **SVBP** — 23 bestemmingshoofdgroepen, 3 dubbelbestemmingen, 8
   gebiedsaanduidinggroepen. Handwerk, eenmalig, ~34 regels.
3. `thema = NULL` voor alles wat geen onderwerp draagt: `functie`,
   `ruimtelijk gebruik`, `beperkingengebied`, `overig`, `wetgevingzone`,
   `overige zone`. Expliciet NULL, niet weglaten — het verschil tussen "weten
   dat het niets zegt" en "niet gekeken" moet in de tabel staan.

### 3.2 Bewijsvolgorde per documenttype

| documenttype | 1e | 2e | 3e |
|---|---|---|---|
| Omgevingsplan, Voorbeschermingsregels | pad | thema-annotatie | activiteit (alleen niveau 2) |
| AMvB, Ministeriële Regeling, Omgevingsverordening, Waterschapsverordening | thema-annotatie | pad | gebiedsaanwijzing |
| Aanwijzingsbesluit N2000, Toegangsbeperkingsbesluit | thema-annotatie | gebiedsaanwijzing | — |
| Wro-plan (IMRO) | bestemmings-/aanduidingsnaam → hoofdgroep | pad | — |
| Vrijetekst (visie, programma, projectbesluit) | thema-annotatie op tekstdeel | pad | gebiedsaanwijzing |

De regel erachter: **de fijnste route die bewijs levert wint**, en waar twee
routes even fijn zijn wint de route die voor dat documenttype het hoogst
gemeten heeft.

### 3.3 Eenheid en overerving

Classificeren op **artikelniveau**; leden erven. De IMOW-annotatie hangt vaak aan
één lid terwijl de bepaling over meerdere leden verdeeld staat (vastgesteld bij
de generator, vault 2026-09-13). Een annotatie op een lid telt dus als bewijs
voor het hele artikel. Voor vrije tekst geldt hetzelfde een niveau hoger: een
annotatie op een divisie werkt door naar de divisieteksten eronder.

### 3.4 Herkomst en onthouden

`v2a.artikel_indeling.herkomst` krijgt nieuwe waarden naast `regels`,
`artikelopschrift` en `beide`:

`thema-annotatie` · `gebiedsaanwijzing` · `activiteit` · `bestemming` ·
`pad+annotatie` (beide routes, eens) · `strijdig` (beide routes, oneens — dan
wint de volgorde uit 3.2 en blijft dit zichtbaar)

Onthouden blijft: geen bewijs = geen categorie. Verwacht blijft dat ~7% van de
Ow-artikelen niets krijgt; dat zijn begrippen, algemene bepalingen en
slotbepalingen.

## 4. Fasering

| fase | werk | klaar als |
|---|---|---|
| **0** | baseline meten | ✅ gedaan 2026-09-16, `meet_indelingsroutes.py` |
| **1** | `core.waardelijst_thema` aanmaken en vullen (IMOW automatisch, SVBP met de hand) | elke waarde uit `p2p.gebiedsaanwijzing.type/groep`, `p2p.activiteit.groep` en `wro.planobject.*hoofdgroep` heeft een rij, ook als die `thema IS NULL` zegt |
| **2** | objectroute in `bouw_indeling.py` voor de zwakke documenttypen (AMvB, MR, verordeningen, N2000, toegangsbeperking) | dekking daar van 0–50% naar ≥95%; omgevingsplan onveranderd (regressietest) |
| **3** | Wro-indeling: naam-match → `bestemmingshoofdgroep` → thema, in een eigen tabel `v2a.wro_artikel_indeling` | ≥18% van alle Wro-artikelen ingedeeld, ≥65% binnen de bestemmingsregels; steekproef van 50 met de hand gecontroleerd |
| **4** | vrijetekst: thema-annotatie op tekstdeel, met overerving over de divisieboom | ≥29% van de divisieteksten ingedeeld, en zichtbaar in de viewer |
| **5** | activiteitnaam als niveau-2-signaal (bruidsschatnamen zijn landelijk identiek) | apart te meten; niet starten voor fase 2 af is |

Fase 2 en 3 zijn los van elkaar te doen. Fase 5 is een eigen onderzoekje en mag
vervallen als fase 2 genoeg oplevert.

## 5. Regressie en acceptatie

Voor elke fase geldt dezelfde meetlat, en die staat er al:

1. `python scripts/meet_indelingsroutes.py --json logs/indelingsroutes-<datum>.json`
   vóór en ná. Dekking mag per documenttype niet dalen.
2. Het omgevingsplan met landelijk pad is de **regressiewacht**: 96,7% dekking en
   de bestaande categorieën. Verandert daar iets, dan is er iets stuk — de
   objectroute hoort dat corpus niet te raken.
3. Overeenstemming pad ↔ object per documenttype meebewegen. Zakt die bij een
   documenttype waar beide routes bestaan, dan is de volgorde verkeerd gekozen.
4. Steekproef met de hand bij elke nieuwe route: 50 artikelen, categorie plus de
   zin waaruit het bewijs komt.

## 6. Risico's

- **Meerdere thema's per artikel.** 7.659 Ow-artikelen krijgen er via objecten
  meer dan één. De indelingstabel is single-label; kiezen op volgorde, of de
  tabel multi-label maken. Nog te beslissen — fase 2 raakt het al.
- **Grofheid van de activiteitengroep.** Daarom staat die route op niveau 2 en
  niet op niveau 1. Wie hem alsnog op niveau 1 zet, haalt de Tanken-fout terug in
  een nieuwe jas.
- **Wro-naam-match is een heuristiek.** 18,9% is een ondergrens; varianten die de
  normalisatie niet vangt, missen stil. Daarom fase 3 met handsteekproef.
- **7,5% van de Wro-artikelen zit in plannen zonder geladen planobjecten.** Of
  dat een laadhiaat is of planvormen zonder objecten, is niet uitgezocht. Eerst
  uitzoeken, dan pas als dekking tellen.

## 7. Wat dit niet oplost

De typeBepaling-as blijft een aparte as met een eigen filter. Dit plan gaat
alleen over het onderwerp. En de indeling wordt er niet *juister* van waar het
pad al werkt — ze wordt breder waar het pad niets had.
