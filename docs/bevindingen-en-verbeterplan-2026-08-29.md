# Bevindingen en verbeterplan — sync 2026-08-28 (run 11)

*Opgesteld 2026-08-29, direct na de sync. Wat de run heeft blootgelegd, en wat we
eraan gaan doen. De uitvoering van die run staat in
[sync-2026-08-28.md](sync-2026-08-28.md); het draaiboek in
[synchronisatie-runbook.md](synchronisatie-runbook.md).*

Deze sync verliep zonder dataverlies en eindigde met beide databases aantoonbaar
gelijk. Toch legde hij **twintig** dingen bloot, waarvan er zes rechtstreeks
betekenden dat productie iets niet toonde wat het had moeten tonen. Dat is geen
toeval en ook geen pech: het is wat er gebeurt als je bij elke stap náloopt of hij
deed wat hij beweerde. De rode draad is telkens dezelfde — **een groene uitkomst
bewijst dat er geen exception was, niet dat het werk klopt.**

---

## 1. De vijf patronen achter de bevindingen

Voordat de lijst: de losse vondsten vallen in vijf soorten. Wie de soort begrijpt,
voorspelt de volgende vondst.

### A. De delta ziet alleen wat langs de delta komt

`repliceer_p2p_naar_prod.py` kopieert de rijen van de expressies die deze run zijn
geladen. Dat is precies goed voor nieuwe data en **principieel blind voor een
backfill**: een landelijke reparatie over de bestaande voorraad raakt geen enkele
expressie en is daarmee onzichtbaar — nu en in elke volgende run.

Zo bleef de hele G-124-reparatie van 09-08 negentien dagen op de lokale machine
staan. `p2p.tekstdeel_hoofdlijn`: **0 op prod, 4.955 lokaal**.

### B. Een teller telt iets anders dan zijn label zegt

Drie keer aangetroffen, in beide richtingen:

- `doel_screen.py` meldt **5.694** sets werkvoorraad; er zijn er **156**.
- De preview meldt **251.410** te embedden elementen; `refresh-v2a` vindt **10**
  dirty regelingen.
- `stand.py` meldt **BIJ** op zes signalen; `verversen.sql` vindt daarna **753**
  gewijzigde teksten.

Het gevaar zit niet in het getal maar in de beslissing eraan vast: op 5.694 sets
zou een fan-out honderdveertig subagents kosten en sla je de stap over.

### C. Twee bestanden die claimen elkaar te spiegelen, doen dat niet

`OCD/dso-loader/src/ddl.py` en `MER-register.nl/sql/mer-schema.sql` zeggen allebei
in hun kop dat ze de andere laag 1-op-1 volgen. Op één avond twee keer betrapt op
het tegendeel — een UNIQUE die de bron weerlegt, en een kolom die niets schrijft
naast de kolom die de scripts verwachten.

### D. Een plan of een statistiek die vastzit

De drieslag van 21-08 (planner koos 36× te traag) en de screening van vannacht
(planner rekende met een lege tabel) zijn hetzelfde verhaal op twee plekken. In
beide gevallen was er geen fout, alleen traagheid — en traagheid ziet er hetzelfde
uit als "veel werk".

### E. Een voorwaarde die nergens staat

`resolve_bronhouder.py` moet gedraaid zijn voordat `add_bg_uniform_to_merdata.py`
iets kan. Dat staat in geen enkele keten, in geen runbook, en de verrijker faalt
er hard op. Gevolg: hij heeft nóóit gewerkt.

---

## 2. De bevindingen

Genummerd B-1 t/m B-20, gesorteerd op impact. "Prod toonde het niet" betekent:
eindgebruikers zagen iets anders dan de werkelijkheid, zonder signaal.

### Prod toonde het niet

| # | Bevinding | Bewijs |
|---|---|---|
| **B-1** | **`p2p.tekstdeel_hoofdlijn` stond op prod op 0** tegen 4.955 lokaal. De G-124-reparatie van 09-08 is nooit op productie aangekomen. | ouders (`hoofdlijn`) wél gelijk: 410 = 410 |
| **B-2** | **Het G-130-gat liep vier tabellen diep**, niet één. Van de 38 ontbrekende gebiedsaanwijzingen ontbrak óók de locatie eronder — alle 38. | 45 `locatie` · 38 `gebiedsaanwijzing` · 349 `tekstdeel` · 199 + 4.955 junctions |
| **B-3** | **`add_bg_uniform_to_merdata.py` heeft nooit gewerkt.** Hij joint op `mer.project.bronhouder_code`; die kolom bestaat pas nadat `resolve_bronhouder.py` hem aanmaakt — een voorwaarde die in geen keten stond. | `UndefinedColumn`; ná de fix 1.271 rauwe BG-strings → 712 canonieke |
| **B-4** | **`mer.project.bronhouder_id` (uit `ddl.py`) staat op 0 van 3.620 gevuld** en wordt door niets geschreven, terwijl de MER-scripts `bronhouder_code` verwachten. | schemadrift tussen `ddl.py` en `mer-schema.sql` |
| **B-5** | **Acht i2a-bronhouders strandden op 503** ná vijf retries. `core.load_run` sluit af op `deels`; niets dwingt herstel af. Zonder ingreep dragen ze de stand van vóór de sync. | `0394 0753 0755 0880 0882 0888 0889 0893`; alle acht in **45 s** hersteld |
| **B-6** | **`publish.py` waarschuwt dat de MER-verrijkers omvallen zonder `OCD_PROD_URL`, en draait door.** De site wordt dan zonder verrijking gepubliceerd. | melding in de log, exit 0 op de andere sites |

### Stille valstrikken in het draaiboek

| # | Bevinding | Bewijs |
|---|---|---|
| **B-7** | **Stap 3 kan niet vóór de post-fase.** `p2p.regeling_load` wordt door `fase_post` geschreven, niet door de p2p-fase. Halverwege draaien repliceert stil de **vorige** set, met exit 0. | droogloop gaf 20 expressies, nul van die nacht |
| **B-8** | **`load-imtr` heeft geen landelijke scope** — het laadt alleen de POC-bronhouder uit `.env`. Er was geen manier om een handvol gestrande gemeenten te herstellen zonder de hele fase van 35 min. | hulptekst zegt "Load toepasbare regels", zonder scope |
| **B-9** | **De exportmappen van de vorige run blokkeren de nieuwe export** (hertaling én judge). Handmatig archiveren was nodig; dat staat nergens. | `bevat al subagent-uitvoer (out-*.jsonl)` |
| **B-10** | **`stand.py` op BIJ is geen vrijbrief.** De vuistregel uit het runbook is nu hard bewezen. | 753 gewijzigde teksten; `screening_cel` 89.061 → 7.266 |
| **B-11** | **De `publish.py`-poort meet exceptions, geen volledigheid** — hij blokkeerde op 8 fouten die al hersteld waren. De MER-poort blokkeert op de *leeftijd* van de nieuwste bronpublicatie en kan een stille bron niet van een stille harvest onderscheiden. | `--force` + `--force-preflight` nodig bij een geslaagde run |

### Prestatie en planners

| # | Bevinding | Bewijs |
|---|---|---|
| **B-12** | **`v2a.tekst_embedding` heeft nooit statistieken gehad.** `n_live_tup = 0` op 1,65 mln rijen, `last_autoanalyze` leeg over de hele levensduur. | screening > 30 s/regel → **2,36 s/regel** na `ANALYZE` (81 s) |
| **B-13** | **psycopg3 pint het queryplan na vijf uitvoeringen** (`prepare_threshold=5`). `ANALYZE` op een draaiend proces helpt dus niet; herstarten hoort erbij. | run bleef traag tot herstart |
| **B-14** | **De p2p→prod-stap schaalt met locaties, niet met regelingen.** 549,6 s voor 47.848 locaties waarvan er **41** echt nieuw waren. | 12,7 min totaal, waarvan 9,2 min één tabel |
| **B-15** | **De bitmapscan-ingreep van 21-08 houdt stand** — geen bevinding maar een bevestiging die telt. | drieslag lokaal 12,0 min · prod 23,0 min · `naammatch_signaal` 8,0 min beide |

### Omgeving en hygiëne

| # | Bevinding | Bewijs |
|---|---|---|
| **B-16** | **Docker Desktop lag eruit bij aanvang — tweede sync op rij.** 21-08 stond de WSL-distro op `Stopped`; 28-08 draaide er geen enkel Docker-proces. | preview kan niet starten zonder DB |
| **B-17** | **De Railway TCP-proxy stond nog open van 21-08** — een week. Stap 9 wordt structureel niet afgemaakt omdat het een dashboard-handeling is. | `--vergelijk-prod` kreeg prod meteen te pakken |
| **B-18** | **De prod-API-sleutel zit niet in `dso-loader/.env`**, dus `/v1/load-status` en `/v1/data-health` konden niet met een HTTP-call worden gecontroleerd. Verificatie is via de database gedaan; de API-laag zelf is niet aangetoond. | `{"detail":"Invalid API key"}` |
| **B-19** | **De trigram-index heet anders op prod dan lokaal** (`idx_te_plain_trgm` tegen `idx_te_inhoud_plain_trgm`), definitie identiek. Cosmetisch, maar elk script dat op naam zoekt breekt aan één kant. | beide zijden precies één index |
| **B-20** | **47 ongerefereerde locaties** staan lokaal wel en op prod niet — 38 Gebiedengroep, 6 Gebied, 3 Ambtsgebied. Dat 38 Gebiedengroepen géén enkel lid in `locatiegroep_lid` hebben is een eigen vraag. | replicatie loopt van regeling omláág; onbereikbaar |

---

## 3. Verbeterplan

Vier sporen, op volgorde van wat het meeste risico wegneemt per uur werk. Elk item
noemt de bevinding, het bestand, en hoe je wéét dat het klaar is.

### Spoor 1 — Sluit de gaten die productie stil onvolledig maken

Dit spoor bestaat omdat B-1 tot en met B-6 allemaal "prod toonde het niet, zonder
signaal" zijn. De volgorde is: eerst de detectie, dan de reparatie. Een reparatie
zonder detectie herhaalt zich.

**1.1 · Een tabelbrede diff lokaal ↔ prod als vaste stap 7** *(B-1, B-2, B-4, B-20)*

De sync had dit gat nooit gevonden zonder handmatig natellen. Maak dat
gereedschap.

- Nieuw: `scripts/diff_lokaal_prod.py` — telt élke tabel in `p2p`, `p2pwijziging`,
  `v2a`, `irm`, `mer`, `vth` aan beide kanten en rapporteert alleen de verschillen.
- Verschillen die *bedoeld* zijn (de 47 ongerefereerde locaties, het
  `p2pwijziging`-verschil uit stap 10) krijgen een **verwachtingsregel** in een
  klein YAML-bestand, zodat de output leeg is als alles klopt en niemand went aan
  ruis.
- Runbook stap 7 verwijst ernaar; het sync-rapport neemt de uitkomst op.

*Klaar als*: een run op de huidige stand precies één regel geeft (`p2p.locatie`,
47) en die regel als verwacht gemarkeerd staat.

**1.2 · Backfills krijgen een eigen overzetpad** *(B-1 — het patroon, niet het geval)*

Dit is de belangrijkste actie in het hele plan, want hij voorkomt de vólgende
B-1. Elke landelijke reparatie op de bestaande voorraad bereikt productie niet
vanzelf.

- Afspraak vastleggen in het runbook én in `CLAUDE.md` van OCD: **wie een backfill
  draait, levert een overzetstap mee.** Geen backfill zonder pad naar prod.
- `backfill_tekstdeel_junctions_prod.py` is het werkende voorbeeld; generaliseer
  hem tot `backfill_naar_prod.py` met een tabel-keten als parameter, zodat de
  volgende backfill geen nieuw script vraagt.
- Nog openstaand geval: **1.852 gebiedsaanwijzingen** hangen landelijk uitsluitend
  aan een tekstdeel. Die komen nu pas mee als hun regeling opnieuw wordt geladen.
  Eén keer doorzetten met hetzelfde script.

*Klaar als*: die 1.852 op prod staan en de afspraak in beide documenten staat.

**1.3 · De MER-keten repareren en gelijktrekken** *(B-3, B-4, B-6)*

- `resolve_bronhouder.py` opnemen in de build-keten van `publish.py`, vóór de twee
  verrijkers.
- `publish.py` moet **falen** in plaats van waarschuwen als `OCD_PROD_URL`
  ontbreekt terwijl er een site is die hem nodig heeft. Een waarschuwing die je
  kunt missen is geen poort.
- `ddl.py` en `mer-schema.sql` gelijktrekken op `bronhouder_code` (text) en
  `bronhouder_id` laten vallen — die is 0 van 3.620 gevuld en heeft geen schrijver.
- Zet een regressietest op het schema: één test die de kolomlijsten van beide
  definities vergelijkt en faalt bij drift.

*Klaar als*: `publish.py --only mer-register` van begin tot eind slaagt zonder
handmatige stappen, en de schematest groen is.

**1.4 · i2a-herstel afdwingen in plaats van onthouden** *(B-5, B-8)*

`i2a_herstel_bronhouders.py` bestaat nu, maar iemand moet hem aanroepen.

- `full_sync.py` laat de gestrande codes achter in `core.load_run.details`.
- De **regressiecheck** in het sync-rapport meldt expliciet: "i2a: 8 bronhouders
  niet geladen — draai `i2a_herstel_bronhouders.py 0394 …" met de codes erbij.
- Overweeg één automatische herkansing aan het eind van de i2a-fase; 45 seconden
  voor acht bronhouders maakt de afweging simpel.

*Klaar als*: een run met kunstmatig gefaalde bronhouders die regel in het rapport
zet, met de juiste codes.

### Spoor 2 — Maak de tellers eerlijk

Alle drie de gevallen van patroon B kosten óf onnodig werk, óf leiden tot een
overgeslagen stap. Ze zijn goedkoop te repareren.

**2.1 · `doel_screen.py` dedupliceren tegen `irm.judge_uniek`** *(B-8 → vault G-132)*
De METING-regel moet hetzelfde getal noemen als `judge_fanout_export`. Verschil nu:
5.694 tegen 156.

**2.2 · De embed-teller in de preview vervangen door de dirty-set** *(B, preview)*
`preview_sync.py` meldt "TE EMBEDDEN: 251.410" waar `refresh-v2a` 10 dirty
regelingen vindt. Laat de preview de dirty-set tonen; het oude getal is
structureel te hoog en wordt daarom genegeerd — en een getal dat je moet negeren
hoort er niet te staan.

**2.3 · `stand.py` een expliciete blinde vlek laten melden** *(B-10)*
Hij kan niet zien dat een nieuw omgevingsplan-artikel de top-K verschuift. Laat
hem dat zélf zeggen, met het aantal sinds de vorige screening geladen
omgevingsplannen erbij: *"BIJ — maar er zijn sindsdien 8 omgevingsplannen geladen;
draai 1a–1d."*

*Klaar als*: alle drie de meldingen kloppen met wat de volgende stap werkelijk aan
werk vindt.

### Spoor 3 — Statistieken en planners

**3.1 · `ANALYZE v2a.tekst_embedding` in de post-fase** *(B-12)*
Nu een runbook-stap; hoort in `full_sync.fase_post`, direct na de embed-fase. Kost
81 s, levert een factor 13 op de eerstvolgende consument.

**3.2 · Uitzoeken waarom autovacuum deze tabel overslaat** *(B-12, vault G-133)*
`last_autoanalyze` is leeg over de hele levensduur van een tabel die per sync
tienduizenden rijen wisselt. Kandidaten: `autovacuum_enabled = off` op tabelniveau,
of de vector-kolom. Dit is de echte oorzaak; 3.1 is het verband eromheen.

**3.3 · `prepare_threshold` bewust zetten in langlopende scripts** *(B-13)*
Elk script dat één statement duizenden keren uitvoert over een tabel die tijdens
de run verandert, zit vast aan het plan van uitvoering nummer vijf. Zet
`psycopg.connect(..., prepare_threshold=None)` waar dat speelt, of herplan
expliciet. Begin bij `tier1_screen.py` en `doel_screen.py`.

**3.4 · De p2p→prod-kopie filteren op wat werkelijk verschilt** *(B-14)*
47.848 locaties over de proxy voor 41 nieuwe rijen. Een hash-vergelijking per
locatie vóór de COPY scheelt naar schatting 9 van de 12,7 minuten, en schaalt mee
als de planvoorraad groeit.

*Klaar als*: 3.1 in de post-fase staat, 3.2 een antwoord heeft (ook als dat "het
is de vector-kolom, niet oplosbaar" is), en 3.4 gemeten sneller is.

### Spoor 4 — Draaiboek en hygiëne

**4.1 · Stap 0 begint met "start Docker Desktop"** *(B-16)*
Twee syncs op rij dezelfde blokkade. Voeg een preflight toe die de engine start als
hij niet draait, en die `wsl --list --verbose` toont als `docker ps` hangt — dat
wijst het in één regel aan.

**4.2 · De proxy-stap uit stap 9 halen of automatiseren** *(B-17)*
Hij blijft liggen omdat hij dashboard-only is. Twee opties: de Railway-CLI
onderzoeken op een `tcp-proxy delete`-equivalent, óf de stap verplaatsen naar het
begin van de volgende sync ("proxy aan") en accepteren dat hij ertussenin open
staat — met dat besluit expliciet opgeschreven in plaats van elke week vergeten.

**4.3 · Exportmappen automatisch archiveren** *(B-9)*
`hertaal_fanout_export.py` en `judge_fanout_export.py` moeten een volle map zelf
wegzetten als `<naam>-<datum>` in plaats van te weigeren. De weigering is terecht
— de batches zijn de referentie voor de natelling — maar de oplossing is
mechanisch en hoort niet handmatig.

**4.4 · De prod-API-sleutel bereikbaar maken** *(B-18)* — **gedaan 2026-08-29**
Zie [bijlage A](#bijlage-a--de-prod-api-sleutel). `OCD_API_KEY_PUBLIC` staat nu in
`dso-loader/.env`; beide endpoints antwoorden. Blijft over: de aanroep opnemen in
runbook stap 7.

**4.5 · Indexnamen gelijktrekken** *(B-19)*
`idx_te_plain_trgm` op prod hernoemen naar `idx_te_inhoud_plain_trgm`. Vijf
seconden werk, voorkomt een verwarrende ochtend.

**4.6 · De Gebiedengroepen zonder leden onderzoeken** *(B-20)*
38 van de 47 ongerefereerde locaties zijn Gebiedengroepen zonder één rij in
`locatiegroep_lid`. Dat past bij de bekende loader-gap rond die tabel en verdient
een eigen meting: hoeveel Gebiedengroepen hebben landelijk geen leden, en klopt
dat met wat de DSO levert?

---

## 4. Volgorde van uitvoeren

Als er tijd is voor drie dingen, doe dan deze drie:

1. **1.1 — de tabelbrede diff.** Zonder detectie vind je de volgende B-1 pas als
   iemand toevallig telt.
2. **1.2 — de backfill-afspraak.** Dit is het enige item dat een héle categorie
   fouten voorkomt in plaats van één geval.
3. **3.1 + 3.2 — de ANALYZE.** Goedkoopste factor 13 in het hele plan, en de
   oorzaak is nog onbekend.

Daarna 1.3 en 1.4 (concrete, afgebakende reparaties), dan spoor 2 (een middag
werk voor drie eerlijke tellers), dan de rest.

**Wat we bewust níét doen**: de 47 ongerefereerde locaties naar prod duwen. Ze
hangen nergens aan; ze overzetten is ruis kopiëren, geen gat dichten. De vraag
áchter die 38 Gebiedengroepen (4.6) is wél de moeite waard.

---

## 5. Wat deze run bevestigde

Niet alles was een probleem. Drie dingen deden precies wat ze moesten:

- **De preview-vs-uitkomst-check.** 10 beloofd, 10 geladen; 6.090 beloofd, 6.090
  geladen; 9 verdrongen, 9 gemarkeerd. Dat is de controle die G-98 vier maanden
  lang had kunnen vangen, en hij werkt.
- **De bitmapscan-ingreep van 21-08.** Lokaal 12,0 min en op prod 23,0 min, met
  `naammatch_signaal` op 8,0 min aan beide kanten — tegen 2u10m en 320,6 min
  ervoor. Twee metingen een week uit elkaar: geen toevalstreffer.
- **De inhoudshash in de doorwerkingsmeting.** Van 13.299 unieke bewijs-sets
  misten er 270 een oordeel — 98% hergebruik. Zonder die hash was dit een run van
  uren geweest in plaats van minuten.

En de natelling op de subagents leverde deze keer **nul** verzonnen sleutels:
940/940, 270/270 en 156/156 gedekt bij de eerste controle. Dat de controle er is
blijft nodig — op 16-08 verzon een agent er wél een — maar het model doet het beter
dan de vorige keer.


---

## 6. Uitvoering — stand 2026-08-29

Het plan is dezelfde dag uitgevoerd. Wat het opleverde staat hieronder; de
verrassing is dat de **detectie** (1.1) meer vond dan de reparaties waarvoor hij
was bedoeld.

### Gedaan

| # | Wat | Uitkomst |
|---|---|---|
| 1.1 | `scripts/diff_lokaal_prod.py` + `diff_verwachtingen.yml` | telt 122 tabellen aan beide kanten, parallel; **26 afwijkingen op de eerste run** |
| 1.2 | Backfill-afspraak in runbook §3a; keten uitgebreid | kaart, kaartlaag en pons erbij; +10 rijen naar prod |
| 1.3 | MER-keten | `resolve_bronhouder.py` in `publish.py`, `.env` wordt geladen, ontbrekende env-var is nu een poort |
| 1.4 | i2a-herstel | het sync-rapport drukt nu het herstelcommando mét codes af |
| 2.1 | `doel_screen.py`-teller | telt nu hetzelfde als de export |
| 2.3 | `stand.py` | zevende signaal: omgevingsplannen die nooit gescreend zijn |
| 3.1 | `ANALYZE` | in `fase_post` én in de preflight, met detectie van lege statistieken |
| 3.2 | Oorzaak autovacuum | **gevonden** — zie hieronder |
| 3.3 | `prepare_threshold=None` | in `tier1_screen.py` en `doel_screen.py` |
| 4.1 | Docker-preflight | start de engine, wijst bij uitblijven WSL aan |
| 4.3 | Exportmappen | archiveren zichzelf naar `<naam>-<datum>` |
| 4.4 | Prod-API-sleutel | bijlage A; beide endpoints antwoorden |
| 4.5 | Indexnaam | prod hernoemd naar `idx_te_inhoud_plain_trgm` |
| 4.6 | Gebiedengroepen | gemeten → nieuwe gap, zie hieronder |
| B-11 | MER-poort | vraagt nu de bron in plaats van de kalender |

### 3.2 — de oorzaak was niet wat G-133 eerst zei

De eerste diagnose was "autovacuum heeft deze tabel over zijn hele levensduur
nooit opgepakt". Dat klopte niet. **PostgreSQL 16 houdt de cumulatieve
statistieken in gedeeld geheugen en gooit ze weg bij een onreine afsluiting.**
Docker Desktop lag eruit toen de sync begon, dus de postmaster startte om
**20:39:22** — drie minuten vóór de run — met een schone lei:

```
pg_stat_database.stats_reset : None
pg_postmaster_start_time()   : 2026-08-28 20:39:22
tabellen zonder last_autoanalyze: 159 van 195
```

Niet één tabel dus, maar 159 van de 195. Autovacuum zag overal "sinds de laatste
analyse niets gewijzigd" en deed niets. Dat maakt B-16 (dode Docker) en B-12
(trage screening) **hetzelfde incident**, twee uur uit elkaar zichtbaar geworden.
`pg_class.reltuples` overleeft zo'n reset, dus de meeste tabellen hielden een
bruikbare schatting — de vectorlaag was de uitzondering omdat hij groot is, elke
sync wisselt, en daarna vooral gelezen wordt.

### Wat de diff op zijn eerste run vond

122 tabellen, 92 gelijk, 26 afwijkend. Na triage bleven er drie over die er
werkelijk toe deden:

1. **`p2p.kaartlaag` (7) en `p2p.kaart` (2) misten op prod** — dezelfde
   tekstdeel-keten als [[G-130]], één tabel verder: alle zeven kaartlagen hangen
   aan een gebiedsaanwijzing die alleen via een tekstdeel bereikbaar was.
2. **`p2p.pons` (3) miste op prod** — en dit corrigeert een conclusie uit
   hoofdstuk 4. Daar stond dat de 47 ongerefereerde locaties "nergens aan hangen,
   dus overzetten is ruis kopiëren". Dat gold voor activiteit, gebiedsaanwijzing,
   tekstdeel en normwaarde — **niet voor pons**, en pons is precies wat
   ponsenkaart.nl toont. Drie van de 47 droegen er een. Die zijn alsnog overgezet;
   het restant is nu 44.
3. **`p2p.locatie_generalisatie` loopt 659.773 rijen achter op prod** — en óók
   lokaal missen 17.230 locaties een generalisatie. `ocd-api/tiles.py` leest die
   tabel vanaf z11, en `vul_locatie_generalisatie.py` draait in **geen enkele
   sync-stap**. Zie hieronder bij "nog te doen".

De rest was verklaarbaar en staat nu met reden in `diff_verwachtingen.yml`:
lokale werktabellen, prod-only tabellen (`mer.project_regeling` wordt tégen prod
gebouwd), de `*_oud`-schaduwtabellen van de swap in stap 6c, run-historie, en de
irm-hit-tabellen die `sync_prod.py` bewust niet meeneemt omdat `build/query.sql`
ze niet leest.

Twee tabellen bleken lokaal gevuld maar op prod niet gelezen:
`vth.omgevingsvergunning_dso` (805) en `vth.vergunning_deeplink` (598). Nagelopen
in `ocd-api/`: geen enkele endpoint raakt ze. De afwijkvergunning-informatie zit
als `afwijk_*`-kolommen ÓP `vergunningkennisgeving`, en die worden wél gepusht.
Enige gevolg is een verouderde teller in `/v1/load-status`.

### 4.6 — 92% van de Gebiedengroepen is leeg

De 38 Gebiedengroepen zonder leden uit de 47-lijst bleken geen curiositeit maar
de landelijke norm: **11.028 van de 11.960 (92,2%)** heeft geen enkele rij in
`p2p.locatiegroep_lid`, en alle 11.028 hebben wél geometrie. Van de 225
bronhouders met Gebiedengroepen hebben er **13** groepen met leden, en dat zijn
vooral provincies. Dat 212 bronhouders consequent lege groepen publiceren terwijl
dertien dat niet doen, is als contentverklaring onwaarschijnlijk. Vastgelegd als
vault-gap **G-135**; de oorzaak (welk API-pad levert de leden?) staat nog open.

### Nog te doen

| # | Wat | Waarom het bleef liggen |
|---|---|---|
| 2.2 | preview-embed-teller vervangen door de dirty-set | kleine wijziging in `preview_sync.py`, niet urgent nu de dirty-set in het runbook staat |
| 3.4 | p2p→prod filteren op wat werkelijk verschilt | 9 van de 12,7 min winst, maar het raakt het hart van de replicatie; verdient een eigen wijziging met eigen verificatie |
| 4.2 | de TCP-proxy-stap | vraagt een besluit, geen code — zie hoofdstuk 4 |
| — | **`vul_locatie_generalisatie.py` inplannen** | volledige herbouw van 20 mln rijen aan beide kanten; een geplande operatie, geen stap die je erbij doet |
| — | **G-135 uitzoeken** | vraagt onderzoek in de Presenteren-API, niet in onze code |

---

## Bijlage A — De prod-API-sleutel

*Toegevoegd 2026-08-29 naar aanleiding van B-18: stap 7 kon de API-laag niet
controleren omdat de sleutel nergens op deze machine stond.*

### Hoe de authenticatie werkt

`ocd-api` leest drie omgevingsvariabelen (`ocd-api/main.py`, regel 44–60):

| variabele | bedoeld voor |
|---|---|
| `OCD_API_KEY_PUBLIC` | client-side viewers (ponsenkaart, vergunningenregister). Te invalideren bij scraper-misbruik zonder backend-clients te raken. |
| `OCD_API_KEY_PRIVATE` | backend-clients zoals de Omgevingsbot. Komt nooit in browsercode. |
| `OCD_API_KEY` | legacy single-key; werkt alleen als de twee hierboven leeg zijn. Staat **niet** op de Railway-service. |

De header heet `X-Api-Key`. `verify_key` accepteert **elke** geldige sleutel en
onderscheidt alleen de *tier* voor logging — `/v1/load-status` en
`/v1/data-health` hangen allebei aan `Depends(verify_key)` zonder tier-eis. Voor
verificatie volstaat dus de **publieke** sleutel; de private is hier niet nodig.

> **Let op, het codecommentaar klopt niet meer.** `main.py` zegt dat de publieke
> sleutel "in de client-side HTML van publieke viewers" zit. Dat gold ooit, maar
> ponsenkaart.nl zet `window.OCD_API_KEY = ''` en houdt de sleutel server-side in
> een Cloudflare Pages Function (`functions/api/[[catchall]].js`, env-var
> `OCD_API_KEY_PUBLIC`). Je kunt hem dus **niet** uit de gepubliceerde site
> plukken — en dat is maar goed ook.

### Waar je hem vandaan haalt

**Route 1 — Railway CLI (snelst).** De CLI is op deze machine ingelogd en aan
project `ocd` / environment `production` gekoppeld:

```bash
cd c:/GIT/OCD
railway variables --service ocd-api --kv | grep OCD_API_KEY_PUBLIC
```

`--kv` geeft `NAAM=waarde`-regels; zonder die vlag krijg je een tabel met
afgekapte waarden. `--service` is verplicht in niet-interactieve shells.

**Route 2 — Railway-dashboard.** Project `ocd` → service **ocd-api** →
*Variables* → oogje bij `OCD_API_KEY_PUBLIC`.

**Route 3 — Cloudflare.** De Pages-projecten dragen dezelfde waarde als
`OCD_API_KEY_PUBLIC`-env-var, maar Cloudflare maskeert secrets ná opslaan. Alleen
bruikbaar als je hem daar zelf net hebt gezet.

### Waar hij hoort te staan

In `dso-loader/.env`, dat door `dso-loader/.gitignore` regel 1 wordt genegeerd en
niet getrackt is. **Nooit** in een bestand dat git ziet — de eerdere
`.env`-in-Cloudflare-deploy heeft laten zien hoe snel dat publiek wordt.

```
# prod-API (Railway service ocd-api) — voor stap 7 van het sync-runbook
OCD_API_KEY_PUBLIC=<32 tekens>
```

### De controle zelf

```bash
K=$(sed -n 's/^OCD_API_KEY_PUBLIC=//p' dso-loader/.env | tr -d '
')
curl -s -H "X-Api-Key: $K" https://ocd-api-production.up.railway.app/v1/data-health
curl -s -H "X-Api-Key: $K" https://ocd-api-production.up.railway.app/v1/load-status
```

Gemeten 2026-08-29, ná deze sync — `data-health` op prod is gelijk aan lokaal:

```
bronhouders 511 · met content 381 · duplicate_naam 0 · pdok_mismatch 0
regelingen_zonder_tekst 0 · dso_mist_totaal 0
geo: vindbare_locaties 320.459 · subdiv_geometrie_null 0 · subdiv_orphans 0
```

Dat `subdiv_orphans` en `subdiv_geometrie_null` allebei op **0** staan is de
onafhankelijke bevestiging dat de `locatie_subdiv`-herbouw op prod (10 bronhouders,
171.053 stukjes) compleet is.

> **Eén ding om niet van te schrikken**: `/v1/load-status` toont voor
> `ozon-regelingen` een `finished_at` van **2026-08-01**. Dat is geen achterstand
> maar het gevolg van runbook-principe 2 — prod krijgt gegevens, geen loaders. De
> laatste échte loader-run op prod dateert van vóór die keuze. Voor `vth` staat er
> wél een verse datum, want dat is een push. Wie `load-status` als
> actualiteitsmeter gebruikt, leest hier dus een verkeerd signaal; overweeg de
> replicatiestappen ook een `core.load_run`-rij op prod te laten schrijven.