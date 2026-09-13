# Herstelplan projectbesluiten — annotatieroute, regelingmodel, herhaalbaarheid

*Opgesteld 2026-09-09, naar aanleiding van de objectversie-meting (vault G-147).
Vervolg op [G-145](../../OmgevingswetKnowledgeBase/vault_v1/gaps.md) en de
`REGELINGMODEL_MAP`-bevinding G-148.*

> ✅ **Uitgevoerd op 2026-09-09**, alle vijf de stappen. Wat er staat blijft de
> verantwoording; hieronder per stap wat het opleverde.
>
> | Stap | Uitkomst |
> |---|---|
> | 1–2 | `_route_uit_links()` + `annotatie_route` in `load_regeling_expand`; de annotatiestap volgt de link, documenttype is nog slechts terugval |
> | 3 | 400-vangnet rond `load_regeltekstannotaties`, met `LEGE_REGELTEKST_STATS` zodat de tellers niet omvallen |
> | 4 | `REGELINGMODEL_MAP["Projectbesluit"] = "RegelingVrijetekst"`; 27 rijen bijgewerkt lokaal **en** op prod; de 7 *Omgevingsplanregels Projectbesluit* blijven compact |
> | 5 | `herlaad_annotaties()` gebruikt dezelfde routekeuze via `bepaal_annotatie_route()` |
>
> **Geverifieerd**: `herlaad_annotaties` op `ws0636/2025/PBCUB` slaagt nu (was
> een 400) en is idempotent — 21 tekstdelen voor en na. De meting over alle 27
> projectbesluiten geeft 0 mislukt en 0 terugvallen: 1.267 tekstdelen, 186
> locaties, 69 gebiedsaanwijzingen. Twaalf regressietests in
> `tests/test_annotatieroute.py`, alle groen; de rest van de suite ook, op één
> falende test in `test_imtr_peildatum.py` die al vóór deze wijziging faalde en
> hier los van staat.

## 1. Wat er aan de hand is

Drie lagen, waarvan er één al geraakt is en twee nog open staan.

### Laag 1 — de route-keuze (G-145, fix werkt niet)

`load_via_api` kiest de annotatieroute op `doc_type`. `Projectbesluit` staat in
`ARTIKELSTRUCTUUR_TYPES` en krijgt dus `load_regeltekstannotaties`. Maar alle 27
vigerende projectbesluiten zijn vrijetekst.

Op 2026-09-05 is daarvoor een terugval ingebouwd (commit `9873065`):

```python
stats = load_regeltekstannotaties(...)
if not stats["regels"] and not stats["activiteiten"]:
    extra = load_divisieannotaties(...)
```

**Die terugval kan niet vuren.** De API antwoordt op deze documenten niet met
een leeg resultaat maar met een harde fout:

```
GET /regelingen/_akn_nl_act_ws0636_2025_PBCUB/regeltekstannotaties
400  {"detail":"Deze regeling ondersteunt geen Regeltekst-objecten."}
```

Getoetst op 2026-09-09, in drie varianten (kaal, `locatieSelectie=primair`, en
met `_expand`): altijd 400. `is_transient()` in `http_retry.py` geeft voor 4xx
terecht `False`, dus de fout gaat direct door. `load_regeltekstannotaties`
**raist**, de regel eronder wordt nooit uitgevoerd, en de buitenste `except`
vangt hem af naar `ANNOTATIE_FOUTEN`.

Direct bewezen:

```
>>> load_regeltekstannotaties(conn, "/akn/nl/act/ws0636/2025/PBCUB", "ws0636", "x")
RAIST: HTTPStatusError Client error '400 Bad Request'
```

De 27 documenten zijn op 05-09 wél hersteld (1.267 tekstdelen), maar via een
losse aanroep, niet via deze route. Het volgende nieuwe of gewijzigde
vrijetekst-projectbesluit valt dus opnieuw om — nu zichtbaar in de
foutentelling in plaats van stil, wat de winst van 05-09 is.

### Laag 2 — het label (G-148)

`REGELINGMODEL_MAP["Projectbesluit"] = "RegelingCompact"`, dus alle 27 rijen in
`p2p.regeling` dragen het verkeerde `regelingmodel`. Het TPOD beschrijft het
projectbesluit als vrijetekst-regeling met optioneel een apart regelingdeel in
artikelstructuur; dat aparte deel is in OCD het eigen documenttype
*Omgevingsplanregels Projectbesluit* (7 rijen, terecht `RegelingCompact`).

Gevolg buiten de loader: `ocd-api/main.py` biedt `regelingmodel` als
filterparameter (`r.regelingmodel = ANY(%s)`) en geeft hem terug in de respons.
Wie op `RegelingVrijetekst` filtert mist vandaag de projectbesluiten. Geen
retrieval-breuk, wel een onjuist antwoord.

### Laag 3 — de data

Al hersteld op 05-09. Stand 2026-09-09: 26 van de 27 hebben tekstdelen
(1.267 in totaal), 0 hebben juridische regels — wat klopt voor vrijetekst. De
enige zonder tekstdelen is *Kadeverbetering Kade Kortland*
(`ws0621/2025-08-15/b6ac202a-…`, 83 tekst_elementen, 0 annotaties); die heeft ze
volgens de ZIP-analyse van 05-09 werkelijk niet. **Geen data-actie nodig.**

> Terzijde onderzocht en géén fout: bij `mnre1182/2025/000009` staat bronhouder
> `mnre1045` en bij `mnre1045/2024/ontwerpPBAramis` staat `mnre1182`. Dat lijkt
> verwisseld maar is precies wat de DSO in `aangeleverdDoorEen` levert.

## 2. De oplossing: de API vertelt zelf welke route klopt

`GET /regelingen/{id}` levert een HAL-link die naar het juiste annotatie-endpoint
wijst:

| Regeling | `type.waarde` | `_links.annotaties` |
|---|---|---|
| `ws0636/2025/PBCUB` | Projectbesluit | **divisie**annotaties |
| `gm0344/2020/omgevingsplan` | Omgevingsplan | **regeltekst**annotaties |
| `pv20/2026/omgevingsvisie` | Omgevingsvisie | **divisie**annotaties |

Dat is de structuurgestuurde routering die G-145 zocht, maar dan uit de bron in
plaats van uit een raadsel. En het kost **geen extra call**: `load_via_api`
doet die detailcall al één stap eerder, in `load_regeling_expand`
(`GET /regelingen/{id}?_expand=true&locatieSelectie=primair`) voor pons en
regelingsgebied. De link ligt daar al op tafel en wordt weggegooid.

### Afweging

| Optie | Voor | Tegen |
|---|---|---|
| Projectbesluit naar `VRIJETEKST_TYPES` | één regel | herhaalt precies de fout die G-145 aanwees: het type voorspelt de structuur niet, en hybride typen bestaan |
| Terugval op de 400 opvangen | klein, dekt ook onbekende typen | reactief: je doet altijd eerst een call waarvan je weet dat hij faalt |
| **`_links.annotaties` volgen** (aanbevolen) | autoritatief, structuurgestuurd, nul extra calls, werkt voor elk toekomstig type | vergt dat `load_regeling_expand` zijn uitkomst doorgeeft aan de annotatiestap |

Aanbevolen: de derde, met de tweede als vangnet. De link is leidend; klapt een
route toch, dan alsnog de andere proberen. Dat dekt ook het geval dat de link
ontbreekt omdat de detailcall zelf faalde.

## 3. Stappen

### Stap 1 — `load_regeling_expand` geeft de route terug

In `src/loaders/api_loader.py`, in `load_regeling_expand`, de HAL-link uitlezen
en meegeven in de bestaande `stats`-dict:

```python
ann = (data.get("_links", {}).get("annotaties", {}) or {}).get("href", "")
stats["annotatie_route"] = ("divisie"   if "divisieannotaties"   in ann else
                            "regeltekst" if "regeltekstannotaties" in ann else None)
```

De functie retourneert nu al een dict met `regelingsgebied` en `pons`; dit is
een derde sleutel, geen signatuurwijziging.

### Stap 2 — de annotatiestap volgt die route

De `if doc_type in ARTIKELSTRUCTUUR_TYPES:`-tak vervangen door een keuze die
eerst naar de route kijkt en op het documenttype terugvalt als de link ontbrak:

```python
route = expand_stats.get("annotatie_route")
if route is None:
    route = "regeltekst" if doc_type in ARTIKELSTRUCTUUR_TYPES else "divisie"
```

Daarna beide takken behouden zoals ze zijn, inclusief de bestaande terugval op
een leeg resultaat (die blijft nuttig voor hybride documenten die wél 200 met
nul regels geven).

### Stap 3 — het vangnet op de 400

De terugval van 05-09 uitbreiden zodat hij ook op de fout reageert, niet alleen
op een leeg resultaat:

```python
try:
    stats = load_regeltekstannotaties(...)
    leeg = not stats["regels"] and not stats["activiteiten"]
except httpx.HTTPStatusError as e:
    if e.response.status_code != 400:
        raise
    stats, leeg = LEEG_STATS, True      # "ondersteunt geen Regeltekst-objecten"
if leeg:
    extra = load_divisieannotaties(...)
```

Zonder dit blijft de fix van G-145 dode code voor precies de documenten waarvoor
hij geschreven is.

### Stap 4 — `regelingmodel` corrigeren

Twee kleine wijzigingen, samen G-148:

```python
REGELINGMODEL_MAP["Projectbesluit"] = "RegelingVrijetekst"
# "Omgevingsplanregels Projectbesluit" blijft RegelingCompact
```

En eenmalig, met dezelfde structuurgestuurde bron als hierboven in plaats van
een harde lijst:

```sql
UPDATE p2p.regeling SET regelingmodel = 'RegelingVrijetekst'
 WHERE documenttype = 'Projectbesluit' AND regelingmodel = 'RegelingCompact';
-- verwacht: 27 rijen (peildatum 2026-09-09)
```

Let op: `core.regelingmodel` is een FK-waardelijst en bevat `RegelingVrijetekst`
al, dus geen DDL nodig. Prod volgt bij de eerstvolgende p2p-replicatie; die
werkt bestaande rijen bij.

### Stap 5 — `herlaad_annotaties` meenemen

`herlaad_annotaties()` in hetzelfde bestand kiest óók op `doc_type in
VRIJETEKST_TYPES` en draait dus `load_regeltekstannotaties` voor een
projectbesluit — met dezelfde 400. Het herstelpad voor remediatie is daarmee net
zo kapot als het laadpad. Dezelfde routekeuze uit stap 2 toepassen.

## 4. Verificatie

```bash
cd c:/GIT/OCD/dso-loader

# 1. Route-keuze klopt voor alle drie de structuren (geen 400 meer)
python scripts/meet_objectversies.py --documenttype Projectbesluit \
  > /tmp/pb.log 2>&1; echo "exit=$?"; tail -20 /tmp/pb.log
# verwacht: 27 regelingen, 0 mislukt, 0 terugvallen

# 2. Label gecorrigeerd
#    SELECT regelingmodel, count(*) FROM p2p.regeling
#     WHERE documenttype='Projectbesluit' AND NOT inactief GROUP BY 1;
#    verwacht: RegelingVrijetekst | 27

# 3. Herlaad-pad werkt: één projectbesluit opnieuw annoteren met force
#    en tellen dat de tekstdelen terugkomen (PBCUB: 21).
```

Regressietest toevoegen naast `tests/test_regelingen_delta.py`: een
`test_annotatieroute.py` die met een gemockte detail-respons vaststelt dat een
`divisieannotaties`-link tot de divisie-route leidt, ook als het documenttype in
`ARTIKELSTRUCTUUR_TYPES` staat.

## 5. Afbakening

- **Geen herlading van de 27.** Hun annotaties staan er al sinds 05-09; dit plan
  gaat over herhaalbaarheid en het label.
- **Geen typelijst-uitbreiding.** `ARTIKELSTRUCTUUR_TYPES` en
  `VRIJETEKST_TYPES` blijven staan als terugval voor het geval de detailcall
  faalde, maar zijn niet langer de eerste bron van waarheid.
- **Blast radius van stap 4**: `regelingmodel` wordt buiten de loader alleen
  gelezen door `ocd-api/main.py` (filterparameter + responsveld) en gezet door
  `ow_loader._detect_regelingmodel` (leest de ZIP en zou het al goed hebben) en
  `scripts/laad_specifieke_regelingen.py` (deelt `REGELINGMODEL_MAP`, volgt dus
  vanzelf).
