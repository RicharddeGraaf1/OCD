# Bevindingen sync 2026-09-12/13

*De sync liep over twee kalenderdagen en drie startpogingen. Dit document
verzamelt wat er onderweg boven kwam. Codewijzigingen die eruit volgden staan
onderaan met hun status.*

---

## 1. `full_sync.py` crashte op zijn eigen rapportageregel — én verborg een controle

**Symptoom.** De eerste run (12-09, 22:20) laadde alle 10 regelingen goed en
crashte daarna:

```
NameError: name 'p2p' is not defined      full_sync.py:402, in _annotatie_regels
```

`fase_post` heeft daardoor niet gedraaid: geen `REGELING_LOAD_BACKFILL`, geen
drieslag-MV's, geen indeling. De harvest zelf was compleet.

**De tweede, ergere helft.** Dezelfde verwijzing staat op regel 462, binnen
`fase_p2p`. Daar *bestaat* `p2p` wel — maar het is `src.pipeline.p2p`, terwijl
`ANNOTATIE_FOUTEN` in `src.loaders.api_loader` woont. `getattr(p2p,
"ANNOTATIE_FOUTEN", [])` gaf dus **altijd een lege lijst**, zonder te klagen.

Die controle is op 2026-09-04 toegevoegd nadat de Zuid-Hollandse
Omgevingsverordening met tekst maar zonder annotatielaag was geladen en het
sync-rapport "0 fouten" meldde. Tussen 04-09 en 13-09 heeft hij **nooit iets
kunnen melden**. Een controle die stil faalt is erger dan geen controle: hij
koopt vertrouwen zonder iets te leveren.

**Status: opgelost.** Beide vindplaatsen wijzen nu naar `api_loader`.
`_annotatie_regels()` importeert de module zelf.

> **Les.** `getattr(obj, "NAAM", default)` is stil per ontwerp. Op een
> module-attribuut dat er *hoort* te zijn, verbergt de default een verkeerde
> import in plaats van hem te melden. Waar de afwezigheid een fout is, hoort
> een directe attribuutverwijzing te staan — of een assert bij het opstarten.

---

## 2. De sweep maalde 119 bronhouders door met een dode database

**Wat gebeurde.** Op 12-09 om 23:52:32 duwde de Microsoft Store WSL 2.7.14.0
door; `wsl.msi` legde `vmmem.exe` om en daarmee de Postgres-container. De
delta-sweep begon acht seconden later.

De preflight was om 23:51:38 nog groen — hij draait één keer, vóór de sweep.
De lus in `api_loader.load_delta` ving elke exception per bronhouder op en ging
door: 119 keer `connection timeout expired`, elk gelogd, de fase die doorliep.

**Status: opgelost.** `MAX_OPEENVOLGENDE_DB_FOUTEN = 5` plus
`DatabaseOnbereikbaar`; alleen `psycopg.OperationalError` telt mee (ook
ingepakt), een geslaagde bronhouder zet de teller terug, DSO-rommel bij één
bronhouder stopt de sweep nooit. Tests in `tests/test_db_noodrem.py`.

Volledige uitwerking en de twee handmatige machine-instellingen:
[werkbank-stabiliteit.md](werkbank-stabiliteit.md).

---

## 3. Leerpunt 0 van het runbook heeft een variant die er nog niet in staat

Het runbook waarschuwt: *"nooit een pipe over het commando waarvan de exitcode
telt"*. De aanbevolen vorm is:

```bash
python scripts/<naam>.py > /tmp/<naam>.log 2>&1; echo "exit=$?"
```

Die vorm heeft **hetzelfde probleem** zodra je hem naar de achtergrond stuurt.
De exitcode van de samengestelde opdracht is die van de laatste — `echo` — en
die slaagt altijd. De crash uit §1 kwam daardoor binnen als *"completed (exit
code 0)"*, terwijl Python op 1 eindigde en de traceback gewoon in het logbestand
stond.

**Doe voor een achtergrondtaak dit**: laat het commando waarvan de exitcode telt
het láátste zijn, en lees de code uit de taakstatus.

```bash
python scripts/<naam>.py > /tmp/<naam>.log 2>&1        # geen `; echo` erachter
```

---

## 4. Een "0" die niets deed — CRLF in een codelijst

Bij het handmatig herbouwen van `locatie_subdiv` meldde
`python -m src.cli refresh-subdiv -b <code>` tien keer *"0 stukjes"*, terwijl
die bronhouders wel degelijk polygonen zonder subdiv hadden.

Oorzaak: de codelijst was met Python in tekstmodus geschreven, dus met CRLF.
`tr '\n' ' '` liet de `\r` staan, waardoor de scope
`LIKE 'nl.imow-gm0118\r.%'` werd — die matcht niets. DELETE 0, INSERT 0, exit 0,
"0 stukjes". Alleen de láátste code werkte, want die had geen afsluitende
newline.

Zelfgemaakt, geen loaderfout. Maar het is dezelfde vorm als G-98: een groene nul
die "niets te doen" lijkt te betekenen en in werkelijkheid "niets gevonden om te
doen" betekent. **Bij een nul die je niet verwachtte: toets tegen een tweede
bron voordat je hem gelooft.** Hier was dat één query op locaties zonder
subdiv-rijen.

Na herstel: alle 10 bronhouders herbouwd, **0 polygonen zonder subdiv**.

---

## 5. `vul_locatie_generalisatie.py` valt om op zijn eigen voortgangsregel

Zodra stdout naar een bestand gaat in plaats van naar een terminal, kiest
Windows cp1252 en crasht de `→` in de voortgangsregel:

```
UnicodeEncodeError: 'charmap' codec can't encode character '→'
```

Werkbare aanroep: `PYTHONIOENCODING=utf-8 python scripts/vul_locatie_generalisatie.py …`

**Nog te doen.** Het script hoort dit zelf te regelen (`sys.stdout.reconfigure(encoding="utf-8")`
bij het opstarten), niet de aanroeper. Andere scripts met rich-uitvoer en
niet-ASCII hebben dit waarschijnlijk ook.

---

## 6. Getallen in het runbook die niet meer kloppen

**`locatie_generalisatie`-duur.** Het runbook noemt 1,8 min voor tien
bronhouders (meting 01-09). Deze run: **46,6 min** voor tien, waarvan pv30
alleen al 22,5 min en pv25 15,6 min. Het verschil is de samenstelling — de
meting van 01-09 betrof tien kleine bronhouders, hier zaten twee provincies bij.
De verwachtingswaarde hoort per bronhoudergrootte te worden uitgedrukt, niet per
aantal.

| bronhouder | rijen | duur |
|---|---|---|
| gm0118 | 102 | 0,7 s |
| gm1949 | 3.281 | 41,6 s |
| ws0663 | 183.070 | 6,1 min |
| pv25 | 463.701 | 15,6 min |
| pv30 | 1.233.734 | 22,5 min |

**`i2a.uitvoeringsregel`.** Stap 5 van het runbook redeneert op 1.238.206
lokaal / 1.232.842 prod (meting 28-08) en concludeert *"verschil triviaal →
laten staan"*. Die getallen zijn achterhaald: commit `282f1ab` (03-09) ruimde
de rijen van de oude loader op die naast de nieuwe waren blijven staan —
1.238.206 oude naast 118.754 nieuwe, 344.504 overbodige rijen weg. Lokaal staat
nu **490.849**, en dat is de schone telling.

`dmn_element` (959.833) en `toepasbaar_regelbestand` (60.376) zijn in dezelfde
periode gewoon gegroeid, wat bevestigt dat het om de opruiming gaat en niet om
verlies.

**Gevolg**: de conclusie van stap 5 kan niet meer op die cijfers rusten. De
lokaal/prod-vergelijking moet opnieuw worden gemaakt zolang de proxy voor stap 3
toch openstaat.

---

## 7. De vth-verrijking is anderhalf uur lang onzichtbaar

`enrich-koop` draait als subproces en schrijft zijn voortgang niet naar het
sync-log. Tussen 09:15 en ~11:25 groeit het log met **nul regels**, terwijl de
stap gewoon werkt. Van buitenaf is "loopt nog" niet te onderscheiden van
"hangt".

Het runbook noemt in §4 wel een teller voor de LLM-stappen (`v2a.hertaling`),
maar geen equivalent voor vth. Dat is:

```sql
SELECT count(*) FILTER (WHERE inhoud_geladen_at IS NOT NULL) AS verrijkt,
       count(*) AS totaal,
       max(inhoud_geladen_at) AS laatste
FROM   vth.vergunningkennisgeving
WHERE  datum_publicatie >= '<eerste dag van deze load>';
```

> **Let op: `inhoud_geladen_at` is UTC**, dus twee uur achter de wandklok in
> zomertijd. Een "laatste rij om 08:15" bij een klok die 10:16 aanwijst is één
> minuut geleden, niet twee uur.

---

## 8. Een stale test — geen regressie

`tests/test_imtr_peildatum.py::test_geen_hardgecodeerde_datum_meer` faalt:

```
assert bron.count('"datum": _peildatum()') == 3
E       assert 2 == 3
```

Commit `590b2a0` verwijderde het hele `activiteitKoppelingen`-blok inclusief
zijn `_peildatum()`-call. Er horen er nu twee te zijn. De peildatum zelf is in
orde — de twee resterende call-sites gebruiken hem netjes.

**Nog te doen**: het getal in de test op 2 zetten, met een regel erbij waarom
het er drie waren.

---

## 9. Waarschuwing over foutfilters — `gm0503` is geen HTTP 503

Tijdens deze run stond er een wachter op het sync-log met `503` in het
grep-patroon, om HTTP-fouten te vangen. Dat patroon matcht **bronhoudercodes**:
`gm0503` (Delft), en net zo goed `gm0503`-achtigen elders. Het leverde een vals
alarm op dat daarna nog even als "er was iets met gm0503" bleef rondzweven.

Een foutfilter over dit log moet op de foutvorm matchen, niet op het getal:
`HTTP 503|Server Error`, niet `503`. Bronhoudercodes bevatten vier cijfers en
komen op vrijwel elke regel voor.

---

---

## 10. Twee locaties die nergens aan hangen — en wat dat over subdiv zegt

Na de replicatie wijken de `locatie_subdiv`-tellingen per bronhouder licht af
tussen lokaal en prod. Twee verschillende oorzaken, en het onderscheid is de
moeite waard.

**gm0828: 593 lokaal tegen 566 op prod — echte ontbrekende locaties.** Oss heeft
lokaal 73 locaties en op prod 71. De twee die ontbreken:

| identificatie | type | noemer |
|---|---|---|
| `nl.imow-gm0828.ambtsgebied.0191a62b…` | Ambtsgebied | Ambtsgebied Gemeente Oss |
| `nl.imow-gm0828.gebiedengroep.dba716ad…` | Gebiedengroep | Raadhuislaan 1K |

Ze zijn **niet** door de replicatie gemist door een gat in de scope-traversal.
Ze worden lokaal door **niets** aangewezen: alle zes de FK's naar `p2p.locatie`
(`locatiegroep_lid` ×2, `activiteit_locatieaanduiding`, `gebiedsaanwijzing`,
`normwaarde`, `tekstdeel`, `pons`) geven nul treffers. Ze zijn met de load
binnengekomen — vermoedelijk als regelingsgebied via `_expand=true` — en er wijst
geen enkele annotatie naar.

~~Gevolg voor prod: onschadelijk, want er is geen consument.~~ **Die conclusie
was fout, en het repo wist het al.** `diff_verwachtingen.yml` zet de marge op
`p2p.locatie` bewust op **nul**, met de reden erbij:

> Een ongerefereerde locatie met geometrie staat gewoon op de kaart.
> `p2p.locatie_subdiv` wordt per bronhouder over **alle** polygoonlocaties
> gebouwd, `p2p.locatie_generalisatie` is daarvan afgeleid en `ocd-api/tiles.py`
> leest dat. Gemeten 2026-09-05: 115 ongerefereerde pv28-locaties (72 Gebied,
> 43 Gebiedengroep, alle met geometrie) waren goed voor 72.262 subdiv-stukjes —
> zichtbaar ontbrekende kaartinhoud in Zuid-Holland.

Referenties doen er voor de kaart dus niet toe. Precies dezelfde redenering die
ik maakte ("niets wijst ernaar, dus niemand ziet het") stond tot 2026-09-05 in
het verwachtingenbestand als marge van 200, en is toen weerlegd en op nul gezet.
Ik heb hem opnieuw gemaakt zonder het bestand te lezen.

**Les**: `diff_verwachtingen.yml` is geen ruisonderdrukker maar een register van
eerder uitgezochte gevallen. Lees de reden vóór je zelf een verklaring bedenkt —
de kans is reëel dat iemand dezelfde vraag al beantwoord heeft, met een meting.

> **Maar het roept een tweede vraag op, en die is niet beantwoord.** De
> Gebiedengroep heet *Raadhuislaan 1K* en de regeling die deze run binnenkwam is
> het *Programma Raadhuiskwartier* van diezelfde gemeente. Dat een locatie met
> die naam door geen enkel tekstdeel wordt aangewezen, ziet er niet uit als
> toeval maar als een ontbrekende koppeling aan de laadkant. Zie ook de bekende
> Gebiedengroep-valkuil (`locatieSelectie=primair`). **Niet onderzocht** —
> het viel buiten deze sync en het is een lokale laadvraag, geen replicatievraag.

**pv30: 430.776 lokaal tegen 430.761 op prod — géén ontbrekende locaties.**
Noord-Brabant heeft aan beide kanten exact 1063 locaties, en toch 15 subdiv-
stukjes verschil. `ST_Subdivide` splitst dus net anders. Lokaal draait PostGIS
3.5 op PG 16.9, prod 3.7 op PG 17.10.

**Les**: een verschil in een *afgeleide* tabel is pas een signaal als de
*bron*tabel gelijk is. Tel eerst `p2p.locatie` per bronhouder; wijkt die niet af,
dan is een verschil in `locatie_subdiv` of `locatie_generalisatie` een
eigenschap van de PostGIS-versie en geen gat.

---

## 11. Richting: geen lokale LLM meer — Sonnet via subagents, niet via de SDK

**Gebruikerskeuze, 2026-09-13.** Er mag niets meer op een lokaal model draaien.
Werk dat een taalmodel nodig heeft loopt op **Sonnet**, en dat hoort expliciet in
de code te staan en niet impliciet in een default.

**De route is subagent-fan-out vanuit de Claude Code-sessie, niet de SDK.**
Dat onderscheid is niet cosmetisch: subagents draaien op het abonnement, terwijl
`from anthropic import ...` API-tegoed kost — en dat tegoed is er niet. Het
runbook schrijft die route bij stap 6bis al voor (`hertaal_fanout_export.py` →
één subagent per batch → `hertaal_fanout_laad.py`), en de 15.455 Sonnet-
hertalingen die al in de cache zitten zijn zo geschreven. Er is nooit een
SDK-call geweest.

Waar deze bevinding hieronder "API" zei, moet dus "subagent op het abonnement"
staan — behalve waar expliciet over tegoed wordt gesproken.

Deze keuze geldt **vanaf nu**; de sync van 13-09 is nog met de bestaande opzet
afgemaakt (de vectorstap draaide al toen de keuze viel).

### Wat er vandaag lokaal draait

| Waar | Model | Soort |
|---|---|---|
| `ocd-api/llm.py` | `qwen2.5:14b` via Ollama — **de default** | generatie |
| `dso-loader` embed-pad (`v2a_refresh`, `run_overnight*`, `build_categorie`, …) | `nomic-embed-text` via Ollama, 12 vindplaatsen | **embeddings** |
| `instructieregels.nl/match/` screening (`tier1_screen`, `doel_screen`, …) | Ollama | embeddings/screening |
| diverse losse scripts (`begrijpelijk-*`, `classify_wijziging`, `2026-07-repair-lege-elementen`) | Ollama | generatie |

### Wat eenvoudig is

`ocd-api/llm.py` heeft de schakelaar al: `OCD_LLM_PROVIDER` kent
`ollama | anthropic | groq | none`. Alleen staat de **default op `ollama`**, en
dat is precies wat de gebruiker niet wil. De ingreep is de default omzetten en de omgeving
expliciet te laten falen in plaats van stilletjes op een lokaal model terug te
vallen.

> **Maar let op de scheiding.** `ocd-api/llm.py` draait *runtime* op productie —
> daar is een subagent geen optie en is de `anthropic`-tak (dus tegoed) of `groq`
> de enige weg. De subagent-route geldt voor het **batchwerk in de sync**
> (hertaling, oordelen): dat draait vanuit een sessie en hoort daar te blijven.
> Eén regel "alles via Sonnet" dekt die twee gevallen niet met dezelfde
> mechaniek; schrijf ze apart op.

> **Let op bij die wijziging**: het default-model voor de anthropic-tak staat op
> `claude-sonnet-4-6`. Dat is een verouderde id — de huidige generatie is
> Claude 5 (`claude-sonnet-5`). Meenemen in dezelfde ingreep.

De losse generatie-scripts zijn stuk voor stuk hetzelfde patroon en kunnen
dezelfde helper gebruiken.

### Wat níét eenvoudig is — en dit moet expliciet zijn

**Anthropic levert geen embeddings-API.** De embed-kant kan dus niet "gewoon via
Sonnet". En het is geen kwestie van een andere aanroep kiezen: het bevraagmodel
moet **exact** het indexmodel zijn, anders zijn de vectoren onvergelijkbaar.

De index is nu `nomic-embed-text` (768 dimensies) en telt **1.653.158 vectoren**:

| source_type | vectoren |
|---|---|
| wro | 646.077 |
| ontwerp | 469.666 |
| Lid | 214.404 |
| Divisietekst | 181.833 |
| Artikel | 63.737 |
| Begrip | 43.058 |
| objectnaam-* | 34.383 |

Van embeddingprovider wisselen betekent dus **de hele index opnieuw opbouwen**,
plus alle bevraagpaden meeverhuizen in dezelfde beweging. Dat is een migratie,
geen configuratiewijziging.

**Drie mogelijke uitkomsten, en dit is een keuze die nog openstaat:**

1. **Embeddings blijven lokaal, generatie gaat naar Sonnet.** De regel wordt dan
   "geen lokaal *taal*model" in plaats van "geen lokaal model". Kleinste
   ingreep, en hij dekt wat de gebruiker in de praktijk raakt: de kwaliteit van
   antwoorden en oordelen.
2. **Embeddings naar een gehoste provider** (Voyage, OpenAI, Cohere — Anthropic
   heeft er geen). Dan hoort daar een model-bake-off bij vóór de migratie, want
   opnieuw wisselen kost daarna 1,65 miljoen vectoren. Er ligt al een openstaand
   voornemen voor zo'n bake-off (bge-m3 / e5 doen het beter op Nederlands).
3. **Retrieval zonder vectoren.** Niet realistisch: de vectorlaag dekt juist de
   norm-as waar de SKOS-trefwoorden blind zijn.

**Aanbeveling**: variant 1 nu vastleggen als de regel, met variant 2 als apart
traject dat een eigen meting en een eigen beslismoment verdient. Anders staat er
een regel in de code die niemand kan naleven.

### Wat er concreet moet gebeuren

- [ ] batchwerk in de sync (hertaling, oordelen): subagent-fan-out op het abonnement — route bestaat al, gebruiken
- [ ] `ocd-api/llm.py` runtime: `OCD_LLM_PROVIDER` default van `ollama` af, en `claude-sonnet-4-6` → `claude-sonnet-5`
- [ ] geen stille terugval naar Ollama: ontbrekende sleutel = harde fout
- [ ] losse generatie-scripts op dezelfde helper zetten
- [ ] `instructieregels.nl` screening: vaststellen of dat embeddings of generatie is (§6b van het runbook zegt embeddings — dan valt het onder de embedding-keuze hierboven)
- [ ] de gekozen variant (1, 2 of 3) vastleggen in het runbook, zodat "geen lokale LLM" een naleefbare regel is

---

## 12. MER-harvest: `http_get` retryt 429/503 maar niet een socket-timeout

`harvest/load_events.py` viel om op pagina ~1250 van 2266:

```
TimeoutError: The read operation timed out
```

`lib.http_get` heeft een nette 429/503-backoff met strikes, `Retry-After` en een
blijvende vertragingsopslag. Maar de `except` vangt alleen
`urllib.error.HTTPError`. Een **socket-timeout** is geen HTTPError, glipt er dus
langs en breekt een harvest van 2.266 items halverwege af.

Onschadelijk in dit geval — de loader upsert op `koop_id` en een volledige run
kost ~40 s, dus opnieuw draaien volstaat. Maar het is wél de reden dat kanaal A
achterliep terwijl niemand iets merkte: `stand.py` meldt "ACHTER", niet "de
vorige poging is halverwege geklapt".

**Nog te doen**: `TimeoutError` en `urllib.error.URLError` meenemen in dezelfde
retry-lus als 429/503. Dat is de tegenhanger van de noodrem uit §2 — daar ging
het om *te lang doorgaan* met een fout die niet overgaat, hier om *te vroeg
stoppen* bij een fout die vanzelf overgaat. Het onderscheid tussen die twee is
precies wat een retry-lus hoort te maken.

---

## 13. `v2a.artikel_indeling` mist zijn foreign key op prod — 72 weesrijen

Na de herbouw van de indeling (stap 6c) telde prod **149.847** ingedeelde
artikelen tegen **149.775** lokaal. Prod had er dus 72 méér, en dat is de
omgekeerde richting van het incident van 13-08.

De speurtocht, in de volgorde die §10 voorschrijft — eerst de bron:

| controle | uitkomst |
|---|---|
| `p2p.regeling` inactief/totaal | 67/2055 aan **beide** kanten, 0 expressies met afwijkende status |
| artikelen in vigerende regelingen | **149.775 = 149.775** |
| `p2p.tekst_element` totaal | **784.415 = 784.415** |
| prod-rijen in een inactieve regeling | 0 |
| prod-rijen die geen Artikel zijn | 0 |
| **prod-rijen zonder bestaand tekst_element** | **72** |

De bron is dus identiek en de 72 extra rijen zijn **wezen**: ze wijzen naar
`tekst_element_id`'s die op prod niet bestaan.

**Dat zou niet kunnen, en daar zit de fout.** De tabel hoort een FK met
`ON DELETE CASCADE` te hebben, zodat een verdwenen tekstelement zijn indelingsrij
meeneemt:

```
LOKAAL: artikel_indeling_tekst_element_id_fkey
        FOREIGN KEY (tekst_element_id) REFERENCES tekst_element(id) ON DELETE CASCADE  (validated)
PROD:   (geen enkele foreign key)
```

Op prod ontbreekt hij volledig. Elke keer dat een expressie door een nieuwe versie
wordt vervangen en de oude tekstelementen verdwijnen, blijven hun indelingsrijen
op prod staan. Het register leest die tabel rechtstreeks, dus het toont
categorieën voor artikelen die er niet meer zijn — nu 72 van 149.847 (0,05%),
maar het loopt elke sync op.

**De fix, en de volgorde is dwingend:** eerst de wezen weg, dán de constraint,
anders faalt het valideren.

```sql
DELETE FROM v2a.artikel_indeling ai
 WHERE NOT EXISTS (SELECT 1 FROM p2p.tekst_element te WHERE te.id = ai.tekst_element_id);

ALTER TABLE v2a.artikel_indeling
  ADD CONSTRAINT artikel_indeling_tekst_element_id_fkey
  FOREIGN KEY (tekst_element_id) REFERENCES p2p.tekst_element(id) ON DELETE CASCADE;
```

**Nog te doen** — dit raakt productiedata (72 rijen verwijderen) en zet er een
constraint op; bewust niet gedaan zonder go.

**Breder**: het loont om dezelfde vergelijking over álle v2a-tabellen te doen.
Als deze FK op prod ontbreekt, is er geen reden aan te nemen dat hij de enige is.
Dat is precies wat `diff_lokaal_prod.py` níét vangt — die telt rijen, geen
constraints.

---

## 14. Eindstand van `diff_lokaal_prod.py` — van 14 naar 5 afwijkingen

Eerste run na stap 6c: **14 AFWIJKEND**. Na drie reparaties tijdens de sync
zelf: **5**. Wat er is gedicht en waarom:

| was | oorzaak | gedaan |
|---|---|---|
| 7× `p2pwijziging.*` | stap 1b was lokaal gedraaid maar `repliceer_p2pwijziging_naar_prod.py` niet — een overgeslagen runbook-stap | gespiegeld: 1.262.390 rijen over 175 besluiten |
| `core.migratie` −1 | migratie alleen op prod geregistreerd | `--markeer-toegepast --target local` |
| `p2p.locatie` +2 | de twee gm0828-locaties uit §10 | overgezet, subdiv + generalisatie herbouwd |

**Wat overblijft, met wat ervan bekend is:**

| tabel | lokaal | prod | wat het is |
|---|---|---|---|
| `p2p.divisie` | 29.167 | 0 | §3a-backfillgat; staat bovendien niet in de tabellijst van de replicatie |
| `v2a.artikel_indeling` | 149.775 | 149.847 | 72 wezen door een ontbrekende FK op prod — zie §13 |
| `p2p.kaartlaag` | 1.154 | 984 | **onverklaard.** Geen wezen aan beide kanten (0 rijen zonder bestaande kaart), dus prod mist 170 rijen waarvan de ouder er wél is. Backfill-klasse, niet van deze run |
| `p2p.tekstdeel` | 29.415 | 29.426 | **onverklaard.** Prod heeft er 11 méér, geen wezen aan beide kanten |
| `v2a.pad_categorie` | 10.537 | 10.544 | **onverklaard.** Prod 7 meer |

De laatste drie zijn klein, bestonden al vóór deze sync, en hebben geen van
alle een wees-verklaring. Ze horen elk een eigen onderzoekje en daarna óf een
reparatie óf een regel in `diff_verwachtingen.yml` — niet blijven staan als
"onverklaard", want dat is precies hoe een controle ophoudt gelezen te worden.

**Nog niet beoordeeld**: *"geometrie-inhoud: 41 bronhouder(s) met een verschil
dat een telling niet ziet"*. Deels het PostGIS-versieverschil uit §10, maar dat
is een aanname tot iemand het per bronhouder toetst.

---

## 15. Wat de schema-diff bij zijn eerste run vond *(golf 1.1 van het vervolgplan)*

`diff_schema_lokaal_prod.py` is gebouwd naar aanleiding van §13 en meteen
gedraaid. Van 173 ruwe verschillen naar **24, allemaal indexen**, en onderweg
drie dingen die het bestaansrecht van de controle bevestigen.

### 15.1 De ontbrekende FK was geen ongeluk maar een patroon

`v2a.artikel_indeling` miste zijn foreign key niet doordat iemand hem vergat aan
te maken, maar doordat **de schaduwtabel-swap hem elke keer weggooit**.
`CREATE TABLE ... LIKE` neemt geen constraints mee, en het herstel erna is
inconsistent:

| script | tabel | PK hersteld | FK hersteld |
|---|---|---|---|
| `indeling_naar_productie.py` | `artikel_indeling` | ✅ (met comment erbij) | ❌ |
| `indeling_naar_productie.py` | `pad_categorie` | ❌ | n.v.t. |
| `2026-08-06-categorie-naar-productie.py` | `chunk_annotatie` | n.v.t. | ❌ |
| `2026-08-06-categorie-naar-productie.py` | `chunk_categorie` | n.v.t. | ✅ |

Eén van de vier deed het goed. Mijn reparatie van §13 zou dus bij de volgende
sync gewoon weer ongedaan zijn gemaakt — en dat is precies wat er de vorige keer
gebeurd moet zijn.

**Gedaan**: alle drie de swaps herstellen nu hun constraints (`NOT VALID` +
`VALIDATE`, zoals `chunk_categorie` het al deed), en de twee ontbrekende
constraints staan alsnog op prod. Schade op dit moment: nul wezen in
`chunk_annotatie`, nul dubbelen in `pad_categorie` — die tabellen worden elke
sync integraal vervangen, dus het gat had nog geen kans gekregen. Dat is geluk
en geen ontwerp.

### 15.2 De vectorindex bestaat niet op productie

`v2a.tekst_embedding` heeft lokaal een HNSW-index
(`USING hnsw (embedding vector_cosine_ops)`); op prod ontbreekt die, bij
1.653.475 rijen. Ook de btree op `regeling_expression` staat er niet.

Wat dat wél en niet betekent, want het is minder dramatisch dan het klinkt:
`ocd-api/semantisch.py` ordent op `embedding <=> …` binnen een CTE die eerst op
scope filtert, dus er wordt geen 1,65 miljoen rijen gescand bij een gewone
aanroep. Maar:

- **lokale prestatiemetingen voorspellen prod niet** voor alles wat de
  vectorkolom raakt — de planner heeft daar een index en hier niet;
- een ongefilterde vectorzoekopdracht zou op prod geen index vinden;
- een HNSW-index over 1,65 miljoen vectoren bouw je niet even tussendoor,
  dus dit is geen knop die je op het moment zelf omzet.

**Niet opgelost** — dit vraagt een meting (hoe vaak en hoe duur is het
vectorpad op prod echt?) en daarna een keuze over de bouwtijd.

### 15.3 Twee ontwerpfouten in de controle zelf, allebei ruis

Het eerste ontwerp gaf 173 meldingen, en dat is er 173 te veel om te lezen:

1. **Een tabel die aan één kant ontbreekt** leverde één regel per kolom, per
   index én per constraint. Van de 78 kolomverschillen waren er 75 in feite
   twaalf tabellen. Nu opgerold tot één regel per tabel.
2. **Indexen vergeleken op naam** gaf dertien valse "prod mist deze index": de
   swap maakt zijn indexen onder een eigen naam aan (`ai_regeling_idx0` tegen
   `artikel_indeling_regeling_idx`), dezelfde index op dezelfde kolom. Nu op de
   definitie zonder de naam.

Verder leest het script de verwachtingen van de rij-diff mee: staat een tabel
daar al met een reden, dan is zijn eenzijdigheid hier ook verwacht. Die reden
twee keer overtypen laat hem verouderen op de plek waar niemand kijkt.

> **En de YAML brak op zijn eerste sleutel.** `constraint:… :: PRIMARY KEY (…)`
> bevat een dubbele punt met spatie, en onquoted maakt dat het hele blok
> onleesbaar — dan is er geen énkele verwachting meer, stil. Exact de val die in
> de vault-`CLAUDE.md` §5.1 beschreven staat en die `model.md` daar tien weken
> heeft gekost. Sleutels staan nu gequote, met de waarschuwing erbij.

### 15.4 Wat er nog openstaat

24 index-verschillen, elk een eigen afweging: 9 die prod mist, 14 die alleen
prod heeft (grotendeels prestatie-indexen die daar terecht kunnen staan) en 1
die inhoudelijk verschilt. Die horen één voor één beoordeeld en dan óf
gerepareerd óf met reden in `diff_schema_verwachtingen.yml` — niet in bulk
weggezet.

## Codewijzigingen uit deze run

| Wijziging | Bestand | Status |
|---|---|---|
| `ANNOTATIE_FOUTEN` naar `api_loader` (2 vindplaatsen) | `scripts/full_sync.py` | klaar, nog niet gecommit |
| Noodrem op 5 DB-verbindingsfouten op rij | `src/loaders/api_loader.py` | klaar, nog niet gecommit |
| Tests voor de noodrem (7) | `tests/test_db_noodrem.py` | klaar, nog niet gecommit |

Nog open, bewust niet in deze run meegenomen:

- encoding zelf regelen in `vul_locatie_generalisatie.py` (§5)
- `test_imtr_peildatum` bijwerken naar 2 call-sites (§8)
- runbook-correcties (§3, §6, §7)
- tempo van `enrich-koop`: 429-afhandeling en adaptieve limiter — zie [enrich-koop-tempo-analyse.md](enrich-koop-tempo-analyse.md)

> Committen gaat naar `main`, en dat is bij OCD meteen een productie-deploy.
