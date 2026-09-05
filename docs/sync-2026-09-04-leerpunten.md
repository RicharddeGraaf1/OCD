# Leerpunten sync 2026-09-04 (run 12)

*Geschreven tijdens en direct na de run. De operationele uitkomsten staan in
`sync-2026-09-04.md`; dit document gaat alleen over wat er misging en wat eraan
te doen valt.*

De run zelf slaagde: 16 regelingen geladen, 0 fouten, regressiecheck schoon. Maar
hij legde elf dingen bloot, en de rode draad is steeds dezelfde: **een controle die
naar tellingen kijkt ziet geen inhoud, en een stap die geen exception gooit heet
"ok".** Dat is de les die dit runbook al drie keer eerder heeft opgeschreven
(G-98, G-117, G-124) en die nu op vier nieuwe plekken terugkwam.

Ze staan hieronder op volgorde van hoe structureel ze zijn, niet chronologisch.

---

## 1. De replicatie is blind voor documenten zónder juridische regels

**De zwaarste van de elf, en de enige die gebruikers zouden zien.**

`repliceer_p2p_naar_prod.py` bouwt zijn scope van boven naar beneden:

```
regeling_load → juridische_regel → activiteit_locatieaanduiding → locatie
                                 → gebiedsaanwijzing            → locatie
                                 → norm → normwaarde            → locatie
                                            tekstdeel ─(locatie in scope?)─┘
```

Elke tak begint bij `juridische_regel`. Een **omgevingsvisie** en een
**programma** hébben geen juridische regels — hun inhoud hangt aan
divisieannotaties (`p2p.tekstdeel`). En `p2p.tekstdeel` heeft geen
regeling-kolom: de enige kandidaat, `divisie_wid`, draagt een
IMOW-identificatie (`nl.imow-gm0363.divisietekst.<uuid>`) terwijl
`tekst_element.wid` een STOP-wId is (`mnre1034_1-0__chp_3__...`). Twee
naamruimten die per definitie niet joinen — gemeten 0 van 27.223 treffers.

Daardoor is de scope **circulair**: een tekstdeel komt alleen mee als zijn
locatie al in scope zit, en die locatie komt alleen in scope via een
gebiedsaanwijzing, en die gebiedsaanwijzing via een juridische regel of via een
tekstdeel. Zit er nergens een juridische regel in de keten, dan komt het hele
cluster nooit langs.

**Wat het kostte, gemeten op deze run.** Zuid-Holland vernieuwde drie
regelingen tegelijk (omgevingsvisie + omgevingsprogramma + verordening):

| tabel | lokaal | prod ná replicatie | mist |
|---|---|---|---|
| `p2p.locatie` (pv28) | 652 | 394 | **258** |
| `p2p.gebiedsaanwijzing` | 5.210 | 5.052 | 158 |
| `p2p.tekstdeel` | 27.817 | 27.468 | 349 |
| `p2p.tekstdeel_gebiedsaanwijzing` | 7.428 | 7.118 | 310 |
| `p2p.tekstdeel_hoofdlijn` | 5.179 | 4.955 | 224 |

De replicatie meldde `Klaar — 29,435 rijen ingevoegd` en exitcode 0.

**Oplossingsrichtingen**, van goedkoop naar goed:

1. **Nu**: `backfill_tekstdeel_junctions_prod.py` als vaste stap ná
   `repliceer_p2p_naar_prod.py` in runbook stap 3, niet als incident-gereedschap.
   Hij loopt precies deze keten en is idempotent. *Kosten: seconden.*
2. **Beter**: de scope van de replicatie een **tweede ingang** geven naast
   `juridische_regel`, namelijk het tekstdeel zelf, gescopet op de bronhouders
   van de geladen regelingen. Ruimer dan nodig, maar `ON CONFLICT DO NOTHING`
   maakt ruimte goedkoop en een gat duur — dezelfde afweging die al in de
   `scope_td`-commentaarregel staat.
3. **Structureel**: uitzoeken of er wél een `tekstdeel → regeling`-route bestaat.
   De divisieannotatie komt uit een Presenteren-call op een expressie, dus die
   herkomst is bekend op het moment van laden; hij wordt alleen niet vastgelegd.
   Een kolom `regeling_expression` op `p2p.tekstdeel` zou dit hele probleem —
   en de `regeling_load.n_locatie`-teller die hierdoor landelijk op 0/NULL staat
   — in één keer oplossen.

---

## 2. "Ongerefereerde locaties zijn onschadelijk" is een onjuiste aanname

In `diff_verwachtingen.yml` staat het verschil op `p2p.locatie` als verwacht,
met marge `[+0, +200]` en de redenering: *"de replicatie bouwt zijn scope vanaf
de geladen regelingen omlaag, dus een locatie waar geen enkele regeling naar
wijst is onbereikbaar."* Die redenering klopt, maar de conclusie die eronder ligt
— dat zo'n locatie dan ook niet getoond wordt — niet.

**De keten is**: `p2p.locatie` → `p2p.locatie_subdiv` → `p2p.locatie_generalisatie`
→ `ocd-api/tiles.py` (z0–z10). En `refresh-subdiv` bouwt **per bronhouder over
álle polygoonlocaties**, niet over de gerefereerde. Een ongerefereerde locatie met
geometrie zit dus gewoon in de tegels.

**Gemeten**: 115 pv28-locaties (72 `Gebied`, 43 `Gebiedengroep`, allemaal met
geometrie) waren goed voor **72.262 subdiv-stukjes** — zichtbaar ontbrekende
kaartinhoud in Zuid-Holland, met noemers als "Bijzondere molenbiotoop" en
"Kantorengebied categorie 1".

**Oplossingsrichting**: behandel `p2p.locatie` als een tabel die **integraal**
gespiegeld wordt in plaats van via de scope. Het zijn 321.096 rijen; de
sleutelvergelijking kost seconden en het verschil is per definitie klein. Pas de
reden in `diff_verwachtingen.yml` aan — hij documenteert nu een aanname die niet
klopt, en dat is erger dan geen reden, want hij pleit een echt gat vrij.

---

## 3. Rijtellingen zien geen inhoudsdrift

Nadat locatie-aantallen gelijk waren, bleef `locatie_subdiv` voor twee
bronhouders afwijken. Oorzaak: **gelijk aantal locaties, andere geometrie**.

| bronhouder | locaties met geometrie | alleen lokaal | **andere geometrie** |
|---|---|---|---|
| gm0392 | 471 / 471 | 0 | **6** |
| gm0376 | 9.128 / 9.128 | 0 | **2** |

Acht locaties, goed voor 4.493 subdiv-stukjes. De oorzaak is dezelfde
delta-logica: de replicatie **upsert alleen locaties in scope**, dus een locatie
waarvan de geometrie wijzigde terwijl hij buiten scope viel, houdt op prod de
oude vorm — voor onbepaalde tijd, want niets komt er ooit nog langs.

`diff_lokaal_prod.py` kan dit per constructie niet zien: hij telt.

**Oplossingsrichting**: een inhoudscontrole naast de telling. Voor geometrie is
`md5(ST_AsBinary(geometrie))` per bronhouder samengevat (bijvoorbeeld
`md5(string_agg(... ORDER BY identificatie))`) goedkoop genoeg om per sync te
draaien, en het geeft één regel per bronhouder in plaats van 321.096
vergelijkingen. Wijkt een bronhouder af, dán pas de rijen ophalen.

---

## 4. `p2p.hoofdlijn` stond in de keten als ouder maar werd nooit gekopieerd

`backfill_tekstdeel_junctions_prod.py` declareert bij `tekstdeel_hoofdlijn` netjes
twee ouders — `p2p.tekstdeel` én `p2p.hoofdlijn` — maar `p2p.hoofdlijn` staat zelf
niet in `KETEN`. Gevolg: 224 rijen bleven twee volledige ronden hangen op
"ouder ontbreekt", geblokkeerd door **14** ontbrekende hoofdlijnen. Precies
dezelfde omissie waarvoor eerder al `p2p.kaart` is toegevoegd.

Gefixt tijdens deze run.

**Oplossingsrichting**: een startup-assertie in het script zelf — elke tabel die
in een `ouders`-lijst voorkomt, moet ook als eigen stap in `KETEN` staan. Dat is
vijf regels code en sluit deze hele klasse fouten.

---

## 5. Migraties hebben geen pad naar productie

`scripts/2026-09-add-generalisatie-prefix-index.sql` was lokaal toegepast en op
prod **niet** — geen van de drie indexen bestond daar. Zonder die
`text_pattern_ops`-index kost een herbouw per bronhouder ~25 s vaste voet, ook
bij 24 rijen, omdat de planner onder `en_US.utf8` niet kan bewijzen dat
`LIKE 'nl.imow-gm0995.%'` een prefix is.

Aangelegd tijdens de run: subdiv 181 s, generalisatie 139 s, planobject 414 s.

Dit is §3a ("een backfill levert zijn eigen pad naar prod mee") toegepast op
**DDL** in plaats van op data. De afspraak staat er wel, maar noemt alleen
data-reparaties.

**Oplossingsrichting**: een migratie-ledger (`core.migratie` met bestandsnaam +
tijdstip + doelwit-DB), zodat "welke migraties mist prod?" een query is in plaats
van een herinnering. `diff_lokaal_prod.py` kan die tabel dan meenemen — het is
precies het soort verschil dat hij hoort te melden.

---

## 6. Scripts in `scripts/` werken alleen via één specifieke aanroeproute

`vul_locatie_generalisatie.py` valt om op `ModuleNotFoundError: No module named
'src'` bij een directe aanroep. Hij werkt alleen vanuit `full_sync.py`, dat
`src` op `sys.path` zet. Het runbook documenteert hem nu juist als los commando:

```bash
python scripts/vul_locatie_generalisatie.py --bronhouder gm0995
```

Dat commando werkt dus niet zoals opgeschreven. Dit is dezelfde familie als
G-125 (`from utils import strip_xml` in `ow_loader.py`) en G-129.

**Oplossingsrichting**: één regel bovenaan elk script in `scripts/`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Of consequent `python -m scripts.<naam>` in het runbook. Het eerste is beter,
want het maakt het commando in het runbook waar in plaats van het runbook aan te
passen aan een beperking.

---

## 7. Een pipe maskeerde een crash als succes

```bash
python scripts/vul_locatie_generalisatie.py $ARGS 2>&1 | tail -30
```

Dit gaf **exitcode 0** terwijl het script direct crashte (punt 6). De exitcode
van een pipeline is die van het láátste commando, en `tail` slaagt altijd. De
traceback stond wél in de uitvoer, maar de statusregel zei "completed".

Dit is de shell-variant van precies wat dit runbook over rapportage zegt: *een
groene status betekent "geen exception in het laatste onderdeel", niet "correct".*

**Oplossingsrichting**: `set -o pipefail` in elk sync-shellscript, en bij
handmatig werk de uitvoer naar een bestand schrijven en de exitcode apart echoën
in plaats van door een pipe te halen.

**Diezelfde avond nog een keer voorgekomen**, ná het opschrijven hierboven: het
health-MV-commando uit runbook stap 3 begint met `psql`, en `psql` staat in deze
omgeving niet op de PATH. `psql: command not found`, en door de afsluitende
`| tail -12` opnieuw **exitcode 0** en de melding "completed". Twee keer dezelfde
val op één nacht is geen toeval maar een eigenschap van de vorm.

Dat legt en passant een tweede ding bloot: **het runbook schrijft op drie plekken
een kale `psql` voor** (health-MV's in stap 3, `verversen.sql` in 6b,
controlequery's in stap 7), en `psql` staat op **geen enkele PATH** — niet in
bash, niet in PowerShell (`Get-Command psql` vindt niets).

Dat het toch al maanden werkt komt doordat de scripts het zelf oplossen, elk op
hun eigen manier:

| route | hoe |
|---|---|
| `sync-hertaling-to-prod.ps1`, `refresh-koop-to-prod.ps1` | expliciet pad: `$psql = Join-Path $PgBin 'psql.exe'`, met een `Test-Path`-controle en een duidelijke fout als hij mist |
| `instructieregels.nl/PLAN.md` (stappen 0a, 0b, 1b) | `docker exec -i dso-postgis psql …` — psql ín de container |
| kale `psql "$PROD_DB_URL" -c …` uit runbook stap 3 | **werkt niet** |

De losse commando's in het runbook zijn dus nooit uitgevoerd zoals ze er staan.
Oplossingsrichting: die drie vervangen door psycopg-equivalenten (zoals ik
vannacht voor de health-MV's heb gedaan) of door dezelfde `$PgBin`-constructie
als de PowerShell-scripts. Een commando in een runbook dat niet draait is erger
dan geen commando, want het suggereert dat de stap gedekt is.

---

## 8. De inhaalslag op de tegellaag had een drempel moeten hebben

Prod bleek 1.124.556 generalisatie-rijen achter te lopen — de lokale volledige
herbouw van 31-08 is nooit overgezet, en geen enkele sync-stap sluit dat gat,
want de replicatie kijkt naar geladen expressies en dit is afgeleide geometrie.
Sommige bronhouders stonden vrijwel leeg:

| bronhouder | lokaal | prod |
|---|---|---|
| ws0655 | 578.935 | **307** |
| pv28 | 610.388 | 206.963 |
| gm0222 | 25.532 | **2** |
| gm0243 | 9.004 | **0** |

Ik heb de 99 afwijkende bronhouders op aflopend verschil herbouwd. Dat was voor
de eerste ~20 volstrekt terecht, maar vanaf ongeveer #50 werd het herbouwen van
0,3–1,35 miljoen rijen (3–9 minuten per bronhouder) om **enkele tientallen**
rijen te corrigeren die geen gat zijn maar **PostGIS-versieruis**: lokaal draait
3.5/PG16, prod 3.7/PG17, en `ST_Subdivide`/`ST_Simplify` splitsen daar net
anders. Herkenbaar aan het teken: bij die staart staat prod er soms *boven*
(ws0653 +68, pv23 +39, ws0539 +38) — een echt gat is altijd eenzijdig.

Bevestigd door de herbouw zelf: 16 van de 63 herbouwde bronhouders gaven
`X weg → X nieuw`, exact hetzelfde aantal als er stond.

**En bevestigd door de hermeting ná het afkappen.** Met 36 van de 99
bronhouders níet herbouwd staat het verschil op **+4.362** — van +1.124.556, dus
99,6% gesloten. Daarvan is 4.002 één bronhouder (gm0392, zie punt 3: identieke
invoergeometrie, andere `ST_Subdivide`-uitkomst) en de rest ±90–200 in **beide**
richtingen. Die 36 overgeslagen bronhouders waren samen dus goed voor vrijwel
niets, terwijl ze naar schatting nog een uur rekentijd hadden gekost. De drempel
uit oplossing 1 is daarmee geen aanname maar een meting.

Na afkappen bleek de spook-backend uit runbook §4 er ook echt te zijn: 63
seconden doorrekenen aan een transactie die nooit zou committen. Opgeruimd met
`pg_terminate_backend`.

**Oplossingsrichtingen**:

1. **Drempel**: herbouw alleen waar `|verschil| > 500` **of** `> 1%` van de
   bronhouder. Dat had hier ~80 bronhouders en ruim een uur bespaard zonder één
   rij echt gat te laten staan.
2. **Leg de ruisband vast** in `diff_verwachtingen.yml` als een *per-bronhouder*
   marge in plaats van een totaalmarge, met de PostGIS-versies erbij als reden.
   Een tweezijdig verschil van enkele tientallen is dan zichtbaar géén signaal.
3. **Zet de stap in de sync**, zodat er nooit meer een achterstand van een
   miljoen rijen kan ontstaan: `fase_post` doet hem sinds 01-09 lokaal per
   bronhouder; de prod-kant heeft dezelfde behandeling nodig in stap 3.

---

## 9. `full_sync.py` viel om op zijn eigen G-133-reparatie

De code die ná de sync van 28-08 is toegevoegd om verloren
statistiek-boekhouding te herstellen, draaide deze run voor het eerst echt — en
crashte:

```
psycopg.ProgrammingError: can't change 'autocommit' now:
connection in transaction status INTRANS
```

`herstel_statistieken_na_herstart()` doet eerst drie `q1()`-selects (waarmee
psycopg3 een impliciete transactie opent) en zet dáárna `conn.autocommit = True`,
wat dan niet meer mag. De sync stopte vóór de snapshot, dus er bleef geen
`sync_run`-rij achter.

Gefixt: autocommit staat nu vóór de eerste query. Daarna deed de stap precies
waarvoor hij bedoeld was — 153 van 198 tabellen zonder statistieken,
`ANALYZE v2a.tekst_embedding` in 65 s.

**Oplossingsrichting**: een reparatie die alleen aanslaat ná een onreine
herstart, draait per definitie zelden. Zulke code hoort een test te hebben die
de conditie forceert (of minimaal een droogloop-vlag), anders is de eerste echte
uitvoering ook de eerste test — en die viel hier samen met een productierun.

---

## 10. De preview onderschat de vth-verrijking structureel

De preview meldde:

```
nog te verrijken (inhoud_geladen_at IS NULL): 25 (~0 min bij 4/s)
```

De werkelijke stap: **122,0 minuten, +6.293 kennisgevingen, 0,9/s** — de duurste
stap van de hele run.

Twee fouten in één regel. Hij telt de **bestaande** achterstand (25) in plaats van
de werkvoorraad die de load er zelf bij zet (6.293) — en die staat één regel
hoger in dezelfde preview. En hij rekent met **4/s** terwijl gemeten 0,9/s wordt
gehaald; run 11 deed 6.090 in ~66 min (1,7/s), dus deze run was ook nog eens
twee keer trager dan de vorige.

**Oplossingsrichtingen**: de schatting baseren op `te laden kennisgevingen +
bestaande achterstand`, en het tempo uit de laatste drie runs halen in plaats van
uit een constante. De trendbreuk 1,7 → 0,9/s is los daarvan het natrekken waard;
op dit moment weet niemand of dat aan KOOP ligt of aan ons.

---

## 11. Mijn eigen fout: SQL splitsen op `;`

Bij het toepassen van het index-script splitste ik op `;` en sloeg statements
over die met `--` beginnen. Het commentaarblok bovenaan het bestand zat vast aan
het eerste `CREATE INDEX`, dus dat statement werd **stilzwijgend overgeslagen** —
twee van de drie indexen aangelegd, en de output zag er compleet uit.

**Oplossingsrichting**: `.sql`-bestanden nooit in Python opknippen. `psql -f`
gebruiken, of het hele bestand als één string aan `cur.execute()` geven —
psycopg3 accepteert meerdere statements prima.

---

## 12. De hertaal-opdracht nodigde uit tot uitbesteden

`OPDRACHT.md` beschrijft nauwkeurig *wat* een subagent moet maken, maar zei niet
dat hij het **zelf** moet doen. Bij 236 teksten per batch koos **3 van de 10**
agents ervoor het werk op te knippen en aan eigen sub-agents uit te delen. Die
liepen vast op de concurrency-limiet, waarna de agent netjes rapporteerde dat hij
"wacht op de achtergrondagents" — en stopte. Resultaat: geen `out-NN.jsonl`, geen
foutmelding, en een eindantwoord dat klinkt alsof er gewerkt wordt.

Dat is dezelfde vorm als punt 7 en 9: een stap die *niets deed* meldt zich niet
als mislukt. Alleen doordat de natelling van `hertaal_fanout_laad.py` op de
sleutelset toetst — en doordat ik tussentijds op schijf keek — kwam het boven.

Gefixt in `scripts/hertaal_fanout_export.py`: het sjabloon draagt de agent nu op
het werk zelf te doen, en in porties van ~40 weg te schrijven zodat een
onderbreking niet het hele batchresultaat kost.

**Oplossingsrichting daarnaast**: `hertaal_fanout_laad.py` zou bij een
volledig **ontbrekend** `out-NN.jsonl` even hard moeten klagen als bij een gat in
de sleutelset. Nu is "bestand ontbreekt" en "batch is klaar" van buitenaf lastig
te onderscheiden zolang je niet telt.

---

## 13. `/v1/load-status` geeft 500 op productie — nog open

Stap 7 schrijft voor om `/v1/load-status` en `/v1/data-health` op de prod-API te
controleren. Het tweede werkt en komt exact overeen met het sync-rapport (511
bronhouders, 381 met content, 39,7% artikeldekking). Het eerste geeft
**Internal Server Error**.

Wat ik heb kunnen vaststellen — de **databasekant is gezond**:

| query uit het endpoint | prod |
|---|---|
| `core.v_load_status` | ok, 0,2 s |
| `core.v_bron_totalen` | loopt vast op `count(*) FROM vth.vergunningkennisgeving` (1.430 MB, 35,6 s) |
| → terugval op `audit.sync_run.totalen` | werkt, 0,0 s, 14 bronnen |
| lopend / laatst_bijgewerkt / bronhouders | ok |
| historie (80 runs, met `(details->>'totaal_na')::bigint`) | ok, geen niet-numerieke waarden |
| sync_runs | ok, 6 rijen |
| **hele reeks achter elkaar, zoals het endpoint hem draait** | **slaagt in 12,7 s** |

Dus: elke query slaagt, het ingebouwde vangnet (8 s eigen timeout → snapshot)
doet aantoonbaar zijn werk, en toch 500't het endpoint. Dat vangnet is er op
2026-07-24 in gekomen (`3c0a0cd`) precies omdát dit anders de hele monitor
omvertrok, en `main` is in sync met `origin/main`.

De fout zit daarmee in de **draaiende API**, niet in de data. Verder uitzoeken
vraagt de Railway-logs van de `ocd-api`-service; die zijn vanuit deze sessie niet
te benaderen.

**Oplossingsrichtingen**:

1. Railway-logs van `ocd-api` erbij pakken op het moment van een call — dat geeft
   de traceback in één keer.
2. Los daarvan is die `count(*)` van 35,6 s het aanpakken waard, ook al vangt de
   code hem nu af: `bron_totaal()` telt een tabel van 1,43 GB op een container
   met 512 MB `shared_buffers`. Een `reltuples`-schatting uit `pg_class` is voor
   een actualiteits-dashboard ruim nauwkeurig genoeg en kost microseconden.
3. Controleer of de `/v1/load-status`-monitor eigenlijk wel gemist wordt. Hij
   voedt het data-actualiteit-dashboard; als die pagina al maanden leeg is zonder
   dat iemand het merkte, is dat op zichzelf een antwoord.

---

## Wat dit bij elkaar zegt

Vier van de elf punten (1, 2, 3, 8) zijn dezelfde fout op verschillende plekken:
**de delta-replicatie is een graaf die vanaf geladen expressies naar beneden
loopt, en alles wat daar niet aan hangt is voor productie onzichtbaar** — voor
altijd, want er is geen tweede mechanisme dat er ooit nog langs komt. Afgeleide
tabellen (subdiv, generalisatie), gedeelde IMOW-objecten (locatie,
gebiedsaanwijzing, tekstdeel) en landelijke backfills vallen er alle drie buiten.

`diff_lokaal_prod.py` is precies daarvoor gebouwd en heeft zich vannacht ruim
terugverdiend — zonder hem was hier niets van opgevallen. Maar hij telt, en
punt 3 laat zien dat tellen niet genoeg is.

De grootste enkele verbetering is waarschijnlijk **punt 1, oplossing 3**: een
`regeling_expression` op `p2p.tekstdeel`. Die maakt de tekstdeel-tak van de scope
net zo gewoon als de andere takken, en repareert en passant de
`regeling_load.n_locatie`-teller die daardoor landelijk leeg staat.
