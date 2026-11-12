# Transitie-export voor Locus Legis

> **Scope**: een export per gemeente van de voorraad van het tijdelijk deel. Die bevat de
> ruimtelijke plannen met plangebied en geldend deel, hun artikelen met onderwerp-indeling en
> bestemmingen, de pons, en één artikel met tekst op verzoek. Het contract is van Locus en staat
> daar: `c:/GIT/Locus/docs/plans/transitieplan/plan-1-voorraad-uit-ocd.md`.
> **NOT covered**: wat Locus met de export doet (rondes, voorrangsregels, voorstellen); het
> verbeteren van de onderwerp-indeling zelf (`docs/onderwerp-as-en-typebepaling-as.md`); de
> converter (`docs/bestemmingsplan-converter.md`).
> **Status**: **Gebouwd** (2026-11-12) op branch `feat/transitie-export`: `ocd-api/transitie.py`,
> `ocd-api/tegels_h.py`, `ocd-api/test_transitie.py`. Uitkomst bij [Uitkomst](#uitkomst-2026-11-12).
> Proefgemeenten: Nunspeet (gm0302, geen pons) en
> Haarlem (gm0392, 6% geponst).

## Aanleiding

Locus Legis krijgt een module Transitie. Daarmee plant een gemeente het omzetten van haar oude
ruimtelijke plannen naar het nieuwe deel van het omgevingsplan, per gebied en per thema. OCD heeft
de grondstof al:

- plannen en plangrenzen (`wro.ruimtelijk_instrument`);
- volledige planteksten met koppelingen naar bestemmingsvlakken (`wro.wro_tekst_object_v2`,
  `wro.wro_tekst_link`, op productie sinds 2026-10-24);
- de onderwerp-indeling per Wro-tekst (`v2a.wro_artikel_indeling`);
- de IMRO-objecten (`wro.planobject`);
- de pons (`p2p.pons`).

Drie dingen ontbreken:

1. Er is geen endpoint dat alle plannen van een gemeente met geometrie levert. `/v1/planvoorraad`
   heeft geen geometrie, `/v1/regelingen/zoek` alleen namen.
2. Niets bepaalt welke plannen in het tijdelijk deel horen. `pons_status` zegt alleen of een plan nog
   in de PDOK-feed staat. Ontwerpen blijven `actief`, en vervangen plannen worden niet weggefilterd.
3. Niets rekent het geldend deel per plan uit: plangebied min de pons.

## Doel

Drie endpoints volgens het Locus-contract, `contractversie` 1, met de privésleutel:

- `GET /v1/transitie/{gemeentecode}/voorraad`
- `GET /v1/transitie/{gemeentecode}/geometrie`
- `GET /v1/transitie/plan/{idn}/artikel/{tekstobjectId}`

## Stappen

1. **Welke plannen.** Leg de selectie vast en meet haar op Nunspeet en Haarlem, plus Waddinxveen
   als controle op inpassingsplannen:
   - de plannen van de gemeente zelf, inclusief de plannen uit de WRO van 1965 ("gemeentelijk
     plan; …");
   - de inpassingsplannen van provincie en Rijk die in de gemeente liggen. Volgens IPLO horen die
     bij het tijdelijk deel (artikel 4.6 lid 1 onder j en k Invoeringswet; vault G-181). Ze staan
     hier met een kunstmatige bronhouder (`gm9921`, `gm0000`, afgeleid uit het IMRO-prefix), dus
     selecteer op ligging en lever de echte bronhouder (`pv..`, `rijk`);
   - geen structuurvisie en geen ontwerp of voorontwerp;
   - per dossier de nieuwste vastgestelde versie. Nunspeet heeft 204 bestemmingsplanversies in 165
     dossiers;
   - weg wat volgens `wro_plan_observatie.relaties` helemaal vervangen is.

   `ontbrekendeSoorten`: beheersverordening, exploitatieplan en voorbereidingsbesluit horen bij het
   tijdelijk deel, maar komen in `ruimtelijk_instrument` niet voor. Lever ze als lijst mee, zodat
   Locus ze als bekend gat toont. Of de IHR ze kan leveren, is een eigen onderzoek.

   Let op: `wro_planvoorraad` haalt nu alleen `planType=bestemmingsplan` op. Wijzigings- en
   uitwerkingsplannen komen uit `ruimtelijk_instrument`.
2. **Artikelniveau.** `object_type` komt uit een heuristiek op de titel, waarin `3.1` op een artikel
   lijkt. Bepaal het artikelniveau uit `niveau`, `parent_id` en `artikel_type`. Toets het op tien
   plannen met de hand tegen ruimtelijkeplannen.nl. Lever per artikel:
   - de kop, het nummer en het hoofdstuk;
   - het aantal leden;
   - de categorie en subcategorie van het artikel;
   - de bestemmingen (via `wro_tekst_link` → `planobject`: naam, `object_type`,
     `bestemmingshoofdgroep`).

   Leden erven hun indeling van het artikel, dus het artikel zelf is de bron. Geen toelichting.

   `tekstenVolledig` is `fout IS NULL` **en** `n_teksten > 0`. In Haarlem hebben alle 56 artikel
   10-plannen geen fout en ook geen tekst. Zodra `docs/wro-teksten-uit-pdf-plan.md` er is, leest de
   export uit `wro.wro_tekst_alle`. Per artikel levert hij dan ook `tekstBron` (`ihr` of `pdf`),
   `pdfPagina` en `gecontroleerd`, en per plan de signalen uit `wro.wro_pdf_bundel`.
3. **Tegels van indeling H.** Per artikel `tegels` en `subonderwerpen`, met dezelfde logica als
   Regels op maat. Die logica is variant E: de categorie, de functies uit het hoofddeel van de
   bestemmingsomschrijving, en trefwoorden in bestemmings- en aanduidingsnamen. Op het tweede
   niveau van H staan de subonderwerpen van de tegel.
   - **Waar de logica nu staat:** in JavaScript in het register
     (`omgevingsdocumentenregister.nl/public/regels-op-maat/thema.js`, `stelselVan`) en in Python in
     de benchmark (`tests/rom-benchmark/run_benchmark.py`, `tegels_e`).
   - **Gedeelde module:** maak er één Python-module van in OCD (`curatie/`) en gebruik die in de
     export. Laat de benchmark hem ook gebruiken, zodat er niet nog een kopie bijkomt.
   - **Bestemmingsomschrijving:** de bestemmingsomschrijving is in Wro meestal een sub-artikel
     (`3.1`). De tegels van een artikel zijn daarom de vereniging over het artikel en zijn
     sub-artikelen.
   - **Lijst en versie:** lever ook de lijst tegels (id, naam, volgorde) en `indelingVersie`.
4. **Geldend deel.** `ST_Difference` van het plangebied met de vereniging van de pons uit de
   nieuwste expressie van het omgevingsplan. Daarbij `geldendM2`, en `plangrens`:
   - `pdok` als `geometrie_herkomst` NULL is;
   - `ihr` bij `ihr-plangrens`;
   - `ambtsgebied` bij `ambtsgebied-imro2006`.
5. **Plangrenzen op productie.** De echte IHR-grenzen voor de 3.736 plannen die de hele gemeente
   als grens hadden, staan sinds 2026-11-04 ook op productie (vault G-177;
   `docs/wro-plangrenzen-draaiboek.md`). Alleen controleren: lever `plangrens: ambtsgebied` alleen
   nog waar `geometrie_herkomst` dat echt zegt. Nunspeet en Haarlem hebben er lokaal 0.
6. **Endpoints** in een eigen router (`ocd-api/transitie.py`). Geometrie in EPSG:28992, vereenvoudigd
   met 2 meter, met `ST_MakeValid` erna.
   Daarnaast `?precisie=exact&idn=…`: de onvereenvoudigde plangrens van de genoemde plannen. Locus
   bouwt daar de pons uit op als een gebied de pons wordt (besluit 2026-11-12). Elke sleutel is natuurlijk: `idn` en
   `(idn, tekstobjectId)`, nooit een id-kolom (`docs/id-drift-tekst-element.md`).
7. **Meten** op Nunspeet en Haarlem:
   - de omvang van beide responses en de responstijd;
   - het aandeel artikelen met een categorie;
   - het aandeel plannen met `plangrens: ambtsgebied`;
   - het aandeel plannen met `tekstenVolledig: false`.

   De uitkomst komt in dit document en gaat naar het Locus-plan.

## Verificatie

- [ ] Op Nunspeet, Haarlem en Waddinxveen komt de lijst plannen overeen met ruimtelijkeplannen.nl, op naam en aantal.
      Afwijkingen zijn verklaard.
- [ ] Tien plannen met de hand: het artikelniveau klopt, geen leden en geen toelichting.
- [ ] Een plan dat half onder de pons ligt, heeft een `geldendM2` van ongeveer de helft, en het
      vlak `geldend` ligt buiten de pons.
- [ ] Voor vijf artikelen zijn de tegels gelijk aan wat Regels op maat op een punt in dat plan toont.
- [ ] Waddinxveen levert de inpassingsplannen Gouweknoop, Extra Gouwekruising en N207-Zuid, met de
      provincie als bronhouder.
- [ ] Geen id-kolom in een response.
- [ ] Alle drie de endpoints weigeren zonder privésleutel.

## Open vragen

1. **Nieuwste pons.** Gepubliceerd geldend, of ook wat al vastgesteld is maar nog niet in werking
   is? Voorstel: geldend, met de expressie in `bron`.

## Uitkomst (2026-11-12)

Gebouwd en gedraaid op de lokale DB voor Nunspeet, Haarlem en Waddinxveen (controle op
inpassingsplannen). Tests: `pytest test_transitie.py`, 12 groen, waaronder de vergelijking van
`tegels_h` met `tegels_e()` uit de RoM-benchmark.

| | Nunspeet | Haarlem | Waddinxveen |
|---|---|---|---|
| Plannen in de export | 189 | 159 | 144, waarvan 2 inpassingsplannen (pv28) |
| Uitgesloten, met reden | 13 (11 TAM, 1 welstandsnota, 1 vervangen) | 2 vervangen | 4 |
| Artikelen | 2.459 | 4.197 | 1.761 |
| Artikelen zonder tegel ("niet ingedeeld") | 1.608 | 2.065 | 1.178 |
| Plannen zonder tekst (signaal) | 7 | 56 | 6 |
| Plannen deels onder de pons | 0 (geen pons) | 39 | 0 |
| Duur voorraad, koude cache | 1–35 s | 1–35 s | 15 s |

**Toets tegen de IHR** (opvragen v4, gefilterd op bevoegd gezag, dezelfde regels):

- **Haarlem:** 159 van de 162 geldende plannen komen overeen. Er ontbreken er 3 in OCD: twee
  bestemmingsplanversies (`-0003`) en één artikel 11-uitwerkingsplan.
- **Nunspeet:** 169 van de 171 komen overeen. Daarnaast heeft OCD 19 plannen die de IHR op 4 en 5
  augustus 2026 verwijderde: de observaties in OCD zijn van 4 juli. Verder mist OCD één nieuwere versie
  (Buitengebied 2023 `-vg04`) en één plan.
- **De selectie zelf klopt.** De verschillen komen doordat OCD achterloopt. Vervolg: een verse
  planvoorraad-snapshot, in het voorkeursvenster, want het gaat landelijk om honderden aanroepen.
- **Soorten die de IHR wél heeft en OCD niet laadt:** exploitatieplan (Nunspeet 2), voorbereidingsbesluit
  (Nunspeet 2) en beheersverordening (Haarlem 7). Ze staan in `ontbrekendeSoorten` en zijn op te halen.
  Laden is een eigen vervolgstap.
- **Projectbesluiten en gerechtelijke uitspraken** staan ook in de IHR (Haarlem 11 en 14). IPLO noemt
  projectbesluiten niet in de lijst van art. 4.6. Een uitspraak kan delen van een plan vernietigen, en
  dat raakt wat er geldt. Beide zijn nog niet verwerkt. ⚠️ Te beoordelen.

**Keuzes die in de code zitten:**

- **TAM-plannen eruit.** Op `is_tam`, en ook op identificatie (`.TAM`) en naam ("TAM - …"), omdat de
  IHR `is_tam` niet altijd zet. Landelijk staan er 1.896 actief.
- **Welstandsnota eruit**, met de reden zichtbaar in `uitgesloten`. Dat is een oordeel, geen meting.
- **Artikel** is de knoop `Artikel` waarvan de ouder geen `Artikel` is. Bepaald via `parent_id`, niet
  via de tekst van het label.
- **Tegels van een artikel:** de eigen indeling van het artikel, de functies uit de
  bestemmingsomschrijving, en van de onderdelen alleen een indeling via een bestemming of aanduiding.
  Zonder die grens kreeg "Begrippen" de tegel van elke begripsbepaling eronder.
- **Bruidsschat:** tegels uit de indeling plus de IMOW-thema's. Gebiedsaanwijzingen en activiteiten
  uit variant E zitten er nog niet in; ze vragen een extra join. Vervolgstap.
- **Signalen per plan:** "tekst ontbreekt" en "platte opbouw". Het laatste is Nunspeet MOLENBEEK: daar
  hangen in de IHR de onderdelen `1.1`, `1.2` direct onder Regels. De export verzint geen artikelen.
- **Nieuwe velden ten opzichte van het contract:** `uitgesloten` (plan, naam, reden), `signalen` per plan,
  `bron.ponsIdentificaties`, `bron.curatieVersie` en `bron.indeling`. Het Locus-plan is bijgewerkt.


## Pons-geschiedenis (2026-11-12)

Locus wil in de tab Gebieden laten zien wat er sinds 1-1-2024 is omgezet. `p2p.pons` kent alleen de
pons van de geldende versie, dus komt er een vierde endpoint bij:
`GET /v1/transitie/{gm}/ponshistorie` (privésleutel).

```json
{"contractversie": 1, "gemeentecode": "gm0392", "gepeild": true,
 "gepeildOp": "2026-11-12T19:58:00+00:00", "crs": "EPSG:28992",
 "stappen": [{"geldigVanaf": "2024-01-01", "geometrieIdentificatie": null, "geometrie": null},
             {"geldigVanaf": "2026-01-01", "geometrieIdentificatie": "e72f…", "geometrie": {"type": "MultiPolygon", "…": "…"}}]}
```

Een stap geldt vanaf `geldigVanaf` tot de volgende. Een gemeente die nog niet gepeild is, geeft
`gepeild: false` en een lege lijst, geen 404.

**Hoe.** De DSO heeft geen lijst met versiedata, maar geeft de pons op een datum terug met `geldigOp`
en `inWerkingOp` (`beschikbaarOp` = nu). `dso-loader/scripts/pons_historie.py` peilt de 1e van elke
maand vanaf 2024-01-01 en zoekt bij een wijziging met bisectie de eerste dag. Opslag in
`p2p.pons_historie` (DDL `dso-loader/scripts/2026-11-12-add-pons-historie.sql`). Peilingen met een
datum gaven die avond reeksen 503's waar een gewone call slaagde; het script wacht dan langer.

**Gemeten (lokaal en prod gelijk):**

| Gemeente | Peilingen | Stappen |
|---|---|---|
| Nunspeet (gm0302) | 36 | 2024-01-01: geen pons, en sindsdien niet |
| Haarlem (gm0392) | 51 | 2024-01-01 geen pons · 2026-01-01 180,7 ha · 2026-09-30 12,1 ha · 2026-10-23 192,9 ha |

De laatste stap van Haarlem is gelijk aan de huidige pons in `p2p.pons` (192,9 ha). De stap van
30-9-2026 is kleiner dan de stap ervoor: de DSO geeft de pons zoals hij in die versie staat, en die is
dus niet altijd groeiend. Voor "omgezet sinds" neemt de afnemer de vereniging van de stappen tot een
datum. Of die versie een fout van de bronhouder is of een bewuste terugzetting, is niet uitgezocht.

**Vervolg.** De overige gemeenten zijn niet gepeild: alle 342 zijn ruim 12.000 calls, dus bulk, en
alleen 22:00-06:00 serieel. Opnemen in de nachtelijke sync als Locus het voor meer gemeenten nodig
heeft.
