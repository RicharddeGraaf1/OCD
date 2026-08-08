# Synchronisatie-runbook OCD

*Opgesteld 2026-08-01. Operationeel plan: wát je draait, in welke volgorde, met
welke go/no-go-momenten.*

Dit is het **draaiboek**. De werking, timings en achtergrond staan in
[synchronisatieproces_beschrijving.md](synchronisatieproces_beschrijving.md) —
die blijft de referentie, dit is de operatie. Bij tegenspraak wint de
beschrijving voor *hoe het werkt* en dit runbook voor *hoe we het doen*.

---

## 1. Principes

1. **Preview vóór elke schrijfactie.** Geen enkele fase gaat draaien zonder dat
   iemand heeft gezien wát hij gaat doen. De aanleiding is concreet: de
   p2p-delta miste maandenlang regelingen terwijl elke run "0 fouten" meldde
   (G-98). Een groene rapportage betekent "geen exceptions", niet "correct".
2. **Prod krijgt gegevens, geen loaders** *(gebruiker-keuze 2026-08-08)*. De
   harvest draait uitsluitend op de lokale werkbank; productie wordt bijgewerkt
   door de geladen rijen van lokaal naar prod te **repliceren**. Draai dus geen
   `full_sync.py --target prod` meer — die stond hier tot 2026-08-08 als de
   aanbevolen route en is dat niet meer. De 80 GB `restore-dev-naar-prod` blijft
   wat hij was: de noodroute, niet de route (G-94).

   Waarom dit beter is dan de loader tegen prod draaien: de bron wordt één keer
   bevraagd in plaats van twee keer, prod en lokaal kunnen per definitie niet
   uiteenlopen doordat ze onafhankelijk van de DSO hebben geladen, en een
   API-hik tijdens de prod-fase kan productie niet meer half gevuld achterlaten.
3. **Fasen zijn losgekoppeld.** Een mislukte deploy mag data nooit raken; een
   mislukte i2a mag p2p niet blokkeren. Elke fase heeft zijn eigen `load_run`.
4. **Zuinig tegen de DSO.** Rate-limiter 50/s; nooit onbegrensd pagineren;
   skip-guard vóór laden. Een volledige lijst-sweep is ~10 calls — goedkoper
   dan de 381 van de per-bronhouder-sweep.
5. **Nooit prunen of publiceren tijdens een load.**

## 2. Rolverdeling van de twee databases

| | LOKAAL (Docker, `localhost:5434/dso`) | PROD (Railway PostGIS) |
|---|---|---|
| Rol | werkbank: evals, viewer-tests, analyses, zware herbouw; **de enige plek waar geharvest wordt** | wat eindgebruikers zien; **ontvangt gegevens, draait geen loaders** |
| Consumenten | jij | ocd-api → viewer, bot, ponsenkaart, instructieregels, vergunningenregister |
| Bereikbaar | altijd | alleen met de **TCP-proxy tijdelijk aan** (dashboard → PostGIS → Settings → Networking) |
| Parallelisme | normaal | `get_conn()` zet het uit (kleine `/dev/shm`) |

Beide moeten actueel zijn. Lokaal achterlaten betekent dat je evals op stale
data draait; prod achterlaten betekent dat de sites stale zijn.

---

## 3. De volgorde

```
0. PREVIEW          read-only, beide DB's            ~1 min
1. LOKAAL laden     p2p + i2a + vth + post           ~1–5 u
2. NABEWERKING      verdrongen versies markeren      ~1 min
3. P2P → prod       rijen repliceren + herbouwen     ~20–40 min
4. VTH → prod       delta-push                       ~10 min
5. I2A → prod       push zodra er iets te pushen is  —
6. EMBEDDINGS       + onderwerp-as, draait standaard uren
6b. DOORWERKING     instructieregels.nl-meting       uren, lokale GPU
6c. ONDERWERP-AS    toewijzingen lokaal → prod       ~1 min
7. VERIFICATIE      beide DB's + API                 ~5 min
8. DOWNSTREAM       gebakken sites herbouwen         ~15 min
9. NAZORG           proxy dicht, VACUUM, loggen,
                    code van de run committen op main ~15 min
```

Stappen 0–4 en 7–9 horen bij elke sync. Stap 5 is een afweging. **Stap 6 draait
standaard mee** — `full_sync.py` slaat hem alleen over met `--skip-embed`, of
stil wanneer Ollama niet bereikbaar is. Stap 6b hangt aan 6: hij zoekt in de
vectorindex, dus zonder embeddings meet hij tegen een index die de nieuwe
regelingen niet kent.

De post-fase domineert de looptijd, niet het laden: `p2p.naammatch_signaal`
alleen al kost 20–35 minuten omdat hij elke objectnaam tegen elke tekst binnen
dezelfde regeling matcht. Bij 16 nieuwe regelingen op 1.990 is een volledige
herbouw feitelijk overdreven — een incrementele of voorwaardelijke refresh is
de grootste openstaande tijdwinst (zie §5).

---

### Stap 0 — Preview (read-only)

```bash
cd c:/GIT/OCD/dso-loader
python scripts/preview_sync.py --vergelijk-prod
python scripts/preview_sync.py --i2a          # optioneel, ~342 calls
```

**Go/no-go — beoordeel drie dingen:**

| Signaal | Betekenis |
|---|---|
| `TE LADEN` ≈ 0 terwijl er weken verstreken zijn | verdacht — dít was G-98. Controleer met `--sinds` leeg (volledige lijst) |
| `VERDRONGEN > 0` | stap 2 is verplicht, anders staan oude en nieuwe versies naast elkaar in de retrieval |
| `VERDWENEN > 0` | noteren, niet automatisch opruimen (G-91) — voorbereidingsbesluiten vervallen van rechtswege, dat is geen intrekking |

Leg de preview-uitkomst vast (kopieer hem in het sync-rapport of de vault-log).
Zonder die vastlegging kun je achteraf niet zien of de sync geladen heeft wat
hij zou laden.

### Stap 1 — Lokaal laden

```bash
python scripts/full_sync.py --label "sync-<datum>" --skip-embed
```

**`--sinds` is sinds 2026-08-08 niet meer nodig.** De p2p-fase draait standaard
over de **volledige lijst**; er is geen watermark meer als default.

De reden staat in de run van 2026-08-07: 7 van de 10 te laden regelingen hadden
een `tijdstipRegistratie` van 2–10 juli, ruim vóór de watermark van 29 juli. Ze
waren ná de vorige run in de DSO-lijst verschenen mét een oud tijdstip — de run
van 1 augustus, die al met `--sinds 2026-06-01` draaide, had ze evenmin gezien.
Een watermark op `tijdstipRegistratie` veronderstelt dat een item zichtbaar
wordt op het moment dat het geregistreerd is, en dat klopt niet.

Het kost ook niets. `find_regelingen_delta` pagineert de volledige lijst hoe dan
ook (~10 calls); `sinds` filtert daarná en bespaart dus geen enkele API-call. De
skip-guard doet het echte werk: een al geladen expressie wordt in ~1,1 ms
herkend, dus ~1.960 keer niets doen kost seconden.

`--sinds` bestaat nog om het venster bewust te knijpen. `--full-p2p` heb je
hiervoor **niet** nodig; dat is de per-bronhouder-sweep, alleen bedoeld na een
verse restore.

Duur: p2p seconden tot minuten (plus ~28 s `locatie_subdiv`-herbouw per
bronhouder mét nieuwe regelingen), i2a ~3 min, vth ~15 min, post (drieslag-MV's)
de lange pool.

### Stap 2 — Verdrongen versies markeren

```bash
python scripts/markeer_verouderde_expressies.py
```

Vraagt per work de vigerende expressie bij DSO op en zet de andere op
`inactief='verouderde-versie'`. Heeft een veiligheidsklep: staat de vigerende
expressie niet in onze DB, dan slaat hij het work over (anders zou het work
volledig verdwijnen). **Draai dit ná stap 1**, niet ervoor — anders slaat hij
precies de works over die je net wilde bijwerken.

Fysiek opruimen is een aparte, bewuste operatie:
`prune_verouderde_versies.py` (dry-run default, `--apply` om te doen).

### Stap 3 — p2p-gegevens naar prod

Vereist de TCP-proxy aan en `PROD_DB_URL` in `.env`.

Prod draait geen loader. Wat stap 1 lokaal heeft geharvest, wordt hier
gerepliceerd: de rijen van de nieuw geladen expressies gaan over de lijn, en
alles wat prod daaruit zélf kan afleiden wordt aan de andere kant herbouwd.

```bash
python scripts/preview_sync.py --target prod    # read-only: wat mist prod?
```

#### Wat kopiëren, wat herbouwen

De scheiding is niet cosmetisch — hij bepaalt of dit 20 minuten of een nacht
kost. Gemeten op 2026-08-07: `p2p` is lokaal **24 GB**, maar de tien nieuw
geladen regelingen beslaan **14.100 `tekst_element` + 6.489 `juridische_regel` +
0 GIO's**. De brondata van een gewone sync past dus ruim in een handvol COPY's;
de omvang zit in afgeleide objecten die je nooit over de lijn moet sturen.

| | Wat | Hoe naar prod |
|---|---|---|
| **Kopiëren** | `p2p.regeling`, `tekst_element`, `juridische_regel`, `locatie`, `activiteit`, `gebiedsaanwijzing`, `kaart`, `geo_informatieobject`, de junction-tabellen daartussen, `p2p.regeling_load` | gefilterde `COPY` op de nieuwe `frbr_expression`-set, in FK-volgorde |
| **Meesturen** | `inactief` / `datum_inactief` / `reden_inactief` van stap 2 | `UPDATE` op de betrokken works |
| **Herbouwen** | `p2p.locatie_subdiv` (12 GB lokaal, ST_Subdivide-afgeleide) | alleen voor de geraakte bronhouders |
| **Herbouwen** | de drieslag-MV's (`naammatch_signaal`, `naammatch_signaal_intra`, `mv_regel_op_locatie`, `tekst_object_consistentie_mv`, `gio_locatie`, `gio_referentie_consistentie_mv`, `ala_punt`) | `refresh_drieslag.py` tegen prod |
| **Herbouwen** | health + stats (`core.mv_bronhouder_health`, `core.mv_geo_health`, `v2a.ponsenkaart_gemeente_stats`) | idem |

`locatie_subdiv` meesturen zou in z'n eentje meer dan de helft van het
p2p-volume over de proxy duwen, voor geometrie die prod in seconden per
bronhouder zelf berekent.

#### Volgorde

1. **Bepaal de set.** De expressies die deze run lokaal zijn geladen:
   `SELECT frbr_expression FROM p2p.regeling_load WHERE geladen_op >= '<start van de run>'`.
   Leg dat lijstje vast — het is de sleutel voor élke volgende stap én voor de
   verificatie.
2. **Kopieer in FK-volgorde**, met `ON CONFLICT DO NOTHING` zodat een herhaalde
   run niets stukmaakt. Ouder-tabellen eerst (`regeling` → `tekst_element` →
   `juridische_regel` → junctions), anders faalt de FK.
3. **Trek de inactief-vlaggen gelijk** voor de works uit stap 2. Sla je dit
   over, dan staan op prod oude én nieuwe versie naast elkaar in de retrieval —
   hetzelfde effect als een overgeslagen stap 2, maar dan alleen op productie.
4. **Herbouw** `locatie_subdiv` voor de geraakte bronhouders, dan de MV's.
5. **Verifieer per tabel** met tellingen aan beide kanten, gefilterd op dezelfde
   expressie-set. Gelijke aantallen = klaar; dit is de tegenhanger van de
   preview-vs-uitkomst-check van stap 7.

#### Gereedschap

```bash
python scripts/repliceer_p2p_naar_prod.py              # droogloop
python scripts/repliceer_p2p_naar_prod.py --ja         # echt
```

Hij bepaalt de expressie-set zelf uit `p2p.regeling_load` (default: sinds de
start van de laatste geslaagde lokale sync; `--sinds` overschrijft dat), volgt de
FK-graaf naar de dimensietabellen, kopieert 27 tabellen in de juiste volgorde en
trekt de inactief-vlaggen gelijk. Gemeten 2026-08-08 voor tien regelingen:
68.796 rijen in scope, ruim 30 seconden.

Vier dingen waar hij op let, elk omdat het bij de eerste run misging:

- **Upsert, geen DO NOTHING.** Bij een nieuwe versie van een bestaand plan
  houden de IMOW-objecten hun `identificatie` (de primaire sleutel) maar krijgen
  ze een nieuwe `regeling_expression`. Met DO NOTHING blijven ze op prod naar de
  oude expressie wijzen: lokaal 6.489 juridische regels voor de tien expressies,
  op prod 244. Nu `ON CONFLICT (pk) DO UPDATE` — lokaal is de waarheid.
- **Identity-kolommen behouden.** `tekst_element.id` is `GENERATED ALWAYS`;
  prod nieuwe id's laten uitdelen zou `tekst_inline_referentie` en
  `v2a.tekst_embedding.tekst_element_id` naar andere tekst laten wijzen. Dus
  `OVERRIDING SYSTEM VALUE`, met de sequence achteraf mee.
- **Generated kolommen weglaten.** `tekst_element.inhoud_plain` is stored
  generated; die invoegen is een harde fout. De staging-tabel wordt daarom uit
  de kolomlijst opgebouwd en niet met `LIKE`.
- **`FORMAT TEXT`, geen `BINARY`.** Lokaal is PG 16.9/PostGIS 3.5, prod PG
  17.10/PostGIS 3.7.

Daarna nog met de hand, want dat is bewust niet in het script gestopt (het is
rekenwerk op prod, geen replicatie):

```bash
# subdiv voor de geraakte bronhouders
OCD_DB_URL="$PROD_DB_URL" python -m src.cli refresh-subdiv -b gm0160   # per code
# dan de MV's
OCD_DB_URL="$PROD_DB_URL" python scripts/refresh_drieslag.py
# health-MV's: zet parallellisme UIT in dezelfde sessie
psql "$PROD_DB_URL" \
  -c "SET max_parallel_workers_per_gather = 0" \
  -c "SET max_parallel_maintenance_workers = 0" \
  -c "REFRESH MATERIALIZED VIEW core.mv_bronhouder_health" \
  -c "REFRESH MATERIALIZED VIEW core.mv_geo_health" \
  -c "REFRESH MATERIALIZED VIEW v2a.ponsenkaart_gemeente_stats"
```

`get_conn()` zet dat parallellisme bij een prod-DSN zelf uit, maar wie
rechtstreeks met `psql`/`psycopg` verbindt moet het zelf doen — de
Railway-container heeft een kleine `/dev/shm`. Gemeten 2026-08-08 op
`core.mv_bronhouder_health`: mét parallellisme `could not resize shared memory
segment … No space left on device`, zonder **16,2 s**.

Gemeten duur op prod (2026-08-08): drieslag **21,6 min** in acht stappen
(`naammatch_signaal` 8,2 · niet-annoteerbaar 3,9 · gio_referentie_consistentie
4,1 · tekst_object_consistentie 2,7 · rest ~2,7), `mv_geo_health` 6,2 min,
`mv_bronhouder_health` 16 s, ponsenkaart-stats 1 s. De "5,5 min lokaal / 11 min
prod" verderop in §5 slaat op de naam-match alleen, niet op de hele fase.

#### Verificatie

Tel aan beide kanten per tabel, gefilterd op dezelfde expressie-set. Gemeten
2026-08-08 ná de upsert-fix, alle negen controles gelijk:

| | lokaal | prod |
|---|---|---|
| regeling | 10 | 10 |
| tekst_element | 14.100 | 14.100 |
| juridische_regel | 6.489 | 6.489 |
| activiteit_locatieaanduiding (via jr) | 8.536 | 8.536 |
| normwaarde | 9.283 | 9.283 |
| locatie_basisgeo | 4.213 | 4.213 |
| locatie met geometrie | 605 | 605 |
| tekst_inline_referentie | 9.276 | 9.276 |
| regelingen inactief (totaal) | 21 | 21 |

Doe deze telling **na** de replicatie en niet ertussendoor: een tussenstand
telt via `juridische_regel` en geeft dan misleidende cijfers.

### Stap 4 — vth naar prod

Alleen als stap 1 nieuwe kennisgevingen laadde.

```powershell
powershell -File scripts/refresh-koop-to-prod.ps1 -Push -Refresh -Verify -ProdUrl "<PROD_DB_URL>"
```

`-Refresh` is **verplicht**, niet cosmetisch: hij doet `VACUUM (ANALYZE)` op
`vth.vergunningkennisgeving` (vult de visibility map) en ververst
`dossier_doorlooptijd` / `vergunning_stats` / `vergunning_stats_type_besluit`.
Zonder die stap lopen `/stats`, `/facets` en `/pins` in de 20 s
statement-timeout.

### Stap 5 — i2a naar prod (afweging)

i2a heeft geen delta en geen push-script. Vergelijk na stap 1 de tellingen
(`i2a.toepasbaar_regelbestand`, `i2a.dmn_element`) tussen lokaal en prod:

- **verschil triviaal** → laten staan tot de volgende gelegenheid;
- **verschil substantieel** → repliceren volgens hetzelfde principe als stap 3
  (rijen van lokaal naar prod). De i2a-tabellen hangen niet aan `frbr_expression`
  maar aan `functionele_structuur_ref` / OIN, dus de filterset is een andere —
  de route is identiek. **Niet** de loader tegen prod draaien.

Twee dingen om te weten vóór je hier tijd in steekt:

- De i2a-loader bevraagt de RTR/STTR met een **hardgecodeerde
  `datum: "10-04-2026"`**. Zolang die er staat, haalt i2a per definitie de
  toestand van 10 april op — een verschil dat níét verschijnt is dus geen bewijs
  van actualiteit.
- **Het per-bestuursorgaan-kanaal levert sinds de initial commit niets op**
  (bevinding 2026-08-07, zie `sync-2026-08-07.md`): de loader stuurt de
  bronhouder-code mét prefix (`gm0363`) terwijl de RTR de kale code verwacht
  (`0363` → 113 activiteiten). Geen activiteiten → geen OIN → STTR wordt
  stilzwijgend overgeslagen. Zolang die fix niet draait, is er domweg niets te
  pushen en is deze stap altijd "verschil triviaal".

### Stap 6 — Embeddings + onderwerp-as

**Deze stap draait standaard mee.** `full_sync.py` roept `fase_embed()` aan
tenzij je `--skip-embed` meegeeft; die start `run_overnight.py`, dat vijf fasen
doorloopt: canonieke chunk-laag → embedden → `chunk_annotatie` →
`chunk_categorie` → objectnamen.

> Tot 2026-08-06 stond hier "niet meenemen in de sync". Dat was achterhaald.
> De eerste reden — `run_overnight.py` doet een `git checkout` + commit — is op
> 2026-08-01 uit het script gehaald; het raakt git niet meer.
>
> De tweede reden (volledige herbouw) is op 2026-08-08 **gemeten en onjuist
> gebleken**. De volledige herbouw is goedkoop:
>
> | Fase | Gemeten |
> |---|---|
> | `chunk_annotatie` (DROP + CREATE, volle corpus) | **4,8 min** — 746.227 rijen over 515.236 chunks |
> | `chunk_categorie` (truncate + opnieuw toewijzen) | **4,9 min** — 750.666 toewijzingen |
> | objectnamen (fase 4b, incrementeel) | 20,5 min — +1.422 chunks |
>
> De uren zitten **niet** in die herbouw maar in fase 4a: die haalt álle
> ~1.979 actieve regelingen op en draait per stuk de recursieve `kop_chain`-CTE
> over `p2p.tekst_element` (3,1 GB), ook voor de regelingen waar de
> `NOT EXISTS`-filter niets oplevert. Gemeten 2026-08-08: **574 van 1.979
> regelingen in 139 minuten** (199 embeddings/min), terwijl Ollama er 25 ms
> over doet — een twaalfde van wat het model aankan. Bij tien gewijzigde
> regelingen is dat 1.969 keer een volledige scan voor niets.
>
> Dat is hetzelfde patroon als de subdiv-storm en `naammatch_signaal`: niet
> "niet incrementeel", maar véél te veel doen. De fix is dus de **detectie**
> scopen (waar `v2a.embed_state` voor is, zie G-97), niet de herbouw.

#### Wat is incrementeel en wat niet

| Fase | Gedrag |
|---|---|
| Fase 3 — canonieke chunk-laag | idempotente DDL, verwaarloosbaar |
| Fase 4a+5 — embedden | **incrementeel**: `NOT EXISTS` op `tekst_element_id`, alleen nieuwe elementen |
| `chunk_annotatie` | **volledige herbouw** (`DROP` + `CREATE`) |
| `chunk_categorie` | **volledige herbouw** (`truncate` + opnieuw toewijzen) |
| Fase 4b — objectnamen | incrementeel (idempotent-filter in Python) |

De belangrijkste eigenschap staat niet in die tabel: **chunks worden alleen
toegevoegd, nooit opgeruimd.** Verdringt de sync een expressie, dan blijven de
chunks van de oude versie staan, mét hun wId's. De onderwerp-as in het register
zeeft die er bij het lezen uit — een categorie telt alleen wId's die in de
getoonde expressie bestaan. Bij `gm0796` bestond op 2026-08-06 **45%** van de
geclassificeerde wId's niet meer in de versie op het scherm. Dat is dus geen
fout maar een oplopende schuld: hoe langer G-97 open staat, hoe meer materiaal
de zeef weggooit.

#### Overslaan mag, maar weet wat je overslaat

```bash
python scripts/full_sync.py --skip-embed     # sync zonder de vectorlaag
python scripts/run_overnight.py              # de stap los, later
```

Sla je hem over, dan is de sync compleet en de vindlaag niet. Nieuw geladen
regelingen zijn dan **niet semantisch vindbaar** én ze missen hun
**onderwerp-categorie** in omgevingsdocumentenregister.nl. Dat tweede is stil:
het filter toont alleen wat het kent, dus een document zonder categorieën ziet
er hetzelfde uit als een document dat nergens over gaat.

Sinds 2026-08-06 rapporteert `fase_embed()` daarom de gemeten verschillen
(chunks, annotaties, toewijzingen, bevestigde categorieën) in het sync-rapport
in plaats van alleen `ok`, en een overgeslagen stap landt in de sectie
**Fouten** in plaats van als losse regel.

#### Naar productie: lokaal draaien en de tabel overzetten

`run_overnight.py` rechtstreeks tegen de prod-DSN draaien kán, maar doe dat
niet. `extend_categorie()` trekt élke embedding naar Python om er numpy op los
te laten; over het internet is dat ~1 miljoen vectoren van 768 floats als
tekst. Lokaal (unix-socket) kost dat 5,3 minuten, over de lijn is het
gigabytes.

De route die wel werkt staat in
`dso-loader/scripts/2026-08-06-categorie-naar-productie.py`: lokaal draaien,
daarna de toewijzingen kopiëren. Gemeten 2026-08-06: **737.911 rijen in 2
seconden**, met een schaduwtabel en een swap, zodat lezers niet blokkeren.
Het script controleert eerst een vingerafdruk over `v2a.tekst_embedding` aan
beide kanten en stopt als die verschilt — anders zouden de `chunk_id`'s naar
andere tekst wijzen. Zonder `--ja` doet hij alleen die controle.

### Stap 6b — Doorwerkingsmeting instructieregels.nl

De monitor op instructieregels.nl toont per instructieregel of hij is uitgewerkt
in de documenten waarop hij zich richt. Die oordelen zitten **niet** in de
loader: ze komen uit een aparte pijplijn in `c:/GIT/instructieregels.nl/match/`
die lokaal draait op Ollama (embeddings + een entailment-judge) en schrijft naar
het `irm`-schema. De site bakt die tabellen alleen maar uit.

Gevolg als je deze stap overslaat: stap 8 herbouwt de site zonder fout, met
oordelen van vóór de sync. Nieuwe instructieregels verschijnen wél in de
inventaris en staan dan op "Onbepaalbaar"; nieuwe omgevingsplan-teksten tellen
helemaal niet mee. De site ziet er precies even actueel uit als daarvoor — dit
is dezelfde soort stille onvolledigheid als G-98.

**Eerst kijken of het nodig is:**

```bash
cd c:/GIT/instructieregels.nl
PYTHONUTF8=1 python match/stand.py        # exit 0 = bij, 1 = achter, 2 = onbepaalbaar
```

Draaien (volledige volgorde staat in `instructieregels.nl/PLAN.md`
§Draaivolgorde; hier het waarom per blok):

```
0a. match/opruimen.sql     verweesde ir_id's weg — anders kost de judge tijd aan
                           instructieregels die niet meer bestaan (426 sets op
                           180 verdwenen regels bij de run van 04-08)
0b. match/verversen.sql    detecteert gewijzigde/verdwenen inhoud en gooit de
                           afgeleide rijen weg, zodat de gewone overslaan-
                           condities ze weer oppakken
1a. tier1_screen.py        embeddings + landelijke top-K screening
1b. match/cellen.sql       screening_hit -> screening_cel (bewijs-hash)
1c. fase2_fill.py          indicatief oordeel per provincie
1d. tier2_fill.py          judge per unieke bewijs-set   <- de lange stap
2a. doel_screen.py         relevantie + screening voor de tier-2-instrumenten
2b. doel_judge.py          judge daarvoor
3.  sync_prod.py           oordelen naar de prod-DB (incrementeel, --dry-run eerst)
```

**Stap 3 hoort hier en niet bij stap 8.** De judge draait alleen lokaal; de
CI-bouw van de site leest de Railway-DB. Zonder die sync deployt stap 8 de
vorige oordelen.

Duur: de eerste volledige cyclus (04/05-08) kostte 78 min screening + 107 min
judge. Bij een gewone sync is het een fractie daarvan, omdat ongewijzigd bewijs
via een inhoudshash zijn bestaande oordeel terugvindt — gemeten 68% hergebruik.

**Wat `stand.py` níét kan zien.** Hij detecteert nieuwe regels, gewijzigde
teksten, verdwenen bewijs en onbeoordeelde bewijs-sets. Hij kan niet zien dat
een *nieuw omgevingsplan-artikel* inmiddels in de top-K van een bestaande regel
zou vallen: de screening is een landelijke top-K per regel, en of die verschoven
is weet je pas door hem opnieuw te draaien. Vuistregel: **laadde stap 1 nieuwe
of gewijzigde omgevingsplannen, draai dan 1a–1d ongeacht wat `stand.py` zegt.**

### Stap 6c — Onderwerp-as naar productie

Stap 6 draait lokaal en laat de productie-DB dus met de vorige toewijzingen
zitten. Het register leest die tabel rechtstreeks, dus zonder deze stap toont
omgevingsdocumentenregister.nl de categorieën van vóór de sync — zonder enig
teken dat er iets ontbreekt.

```bash
cd c:/GIT/OCD/dso-loader
python scripts/2026-08-06-categorie-naar-productie.py        # droogloop
python scripts/2026-08-06-categorie-naar-productie.py --ja   # echt
```

De droogloop vergelijkt een md5 over alle `v2a.tekst_embedding`-id's aan beide
kanten. Komt die niet overeen, dan stopt hij: de `chunk_id`'s zouden dan naar
andere tekst wijzen. Dat is precies het geval **als je stap 6 lokaal hebt
gedraaid en er nieuwe chunks bij zijn gekomen** — dan moet eerst de
embedding-tabel zelf mee naar prod, anders klopt de sleutelruimte niet.

Wat hij doet: `v2a.categorie` gelijktrekken (alleen `UPDATE`, de 99 id's zijn
aan beide kanten dezelfde), de toewijzingen in een schaduwtabel laden en die
omwisselen. De oude tabel blijft achter als `v2a.chunk_categorie_oud`, dus
terugdraaien is twee renames. Gemeten 2026-08-06: 737.911 rijen in 2 s laden,
swap in 1 s, foreign keys herstellen 2 min.

### Stap 7 — Verificatie

```bash
python scripts/preview_sync.py --vergelijk-prod    # moet nu ~0 te laden tonen
```

En verder:

- `SYNC-REPORT-<datum>.md` — 0 fouten? Komt "nieuw geladen regelingen" overeen
  met wat de preview voorspelde? **Dit is de belangrijkste check**: preview
  versus uitkomst is de enige die stille onvolledigheid vangt.
- `audit.sync_run` — `klaar_op` gevuld (anders toont het dashboard een
  spook-sync).
- `core.load_run` — geen rij op `running` blijven staan.
- `/v1/load-status` en `/v1/data-health` op de prod-API.

### Stap 8 — Downstream

Live sites (ponsenkaart-API-proxy, prod-viewer, bot) volgen prod automatisch.
Gebakken sites moeten herbouwd:

```bash
python scripts/publish.py                 # dry-run (default)
python scripts/publish.py --execute       # instructieregels, ponsenkaart, RoM
```

De poort van `publish.py` laat alleen door als de laatste sync-run "0 fouten"
meldde. Zie de kanttekening in §5: die poort meet exceptions, geen
volledigheid.

Vóór instructieregels bouwt draait `publish.py` een pre-flight op de
doorwerkingsmeting (`match/stand.py`, stap 6b). Staat die op ACHTER, dan wordt
de site **overgeslagen** in plaats van met verouderde oordelen gepubliceerd, en
eindigt `publish.py` met exitcode 1.

Overrulen kan met `--force-preflight` — bewust een **andere** vlag dan
`--force`. Als één vlag beide poorten dekte, zou iedereen die langs een rode
sync-status moet (en dat is vaker dan je denkt) de doorwerkingspoort ongemerkt
meesleuren.

Voor instructieregels is een `wrangler`-deploy overigens niet de enige weg: de
repo deployt sinds 04-08 ook op elke push naar `main` (GitHub Actions bouwt dan
uit de Railway-DB via `secrets.PSQL_CONN`). Wie na stap 6b iets in die repo
commit, publiceert dus al.

### Stap 9 — Nazorg

- **TCP-proxy weer UIT** in het Railway-dashboard. De DB-proxy hoort dicht te
  staan tussen operaties door.
- `VACUUM` op de lokale DB als er geprund is (dead tuples; lokaal 86,5 GB
  tegenover 59 GB op prod).
- Sync-rapport + preview-uitkomst vastleggen; vault `log.md` bijwerken en
  `gaps.md` als er iets nieuws bovenkwam.
- **Code die tijdens de run is gewijzigd committen — op `main`.** Een sync legt
  regelmatig een loader-fout bloot (2026-08-01: de delta-sortering én de
  ontbrekende retry). Die fixes horen dezelfde dag op main, niet op een branch
  die maanden blijft hangen. Zie de repo-hygiëne hieronder.
- **Nooit tijdens een lopende run**: een `git checkout` wisselt de code onder
  de draaiende processen. Wacht tot het rapport geschreven is.

---

## 3b. Repo-hygiëne: één branch, `main`

De sync raakt loader-code, en loader-code hoort op één plek te staan. De regel
is simpel: **`main` is de waarheid; werk erop, commit erop, deploy ervan.**
Feature-branches zijn kortlopend of ze bestaan niet.

Stand van zaken (gemeten 2026-08-01, corrigeert
[branch-consolidatie-2026-07-18.md](branch-consolidatie-2026-07-18.md)):

| Branch | T.o.v. main | Oordeel |
|---|---|---|
| `fix/vergunningen-hot-path` | **identiek** (0/0) | naam-restant; werk gewoon op main |
| `feat/rp-planvoorraad` | 63 achter, 1 vooruit | feature staat al op main → **weg** |
| `feat/retrieval-kernel-convergentie` | 63 achter, 34 vooruit | 0 unieke commits t.o.v. vector-chunk-lagen → **weg** |
| `feat/vector-chunk-lagen` | 63 achter, 44 vooruit | bevat als enige `ocd-api/intent.py`, `rank.py`, `fastpaths.py` + golden set + eval-tooling |

Anders dan de julinotitie stelt, is main dus **niet** meer de achterloper: de
wro-loaders, de BOPA-loader, `vergunningen.py` (incl. doorlooptijd) en de
schone vector-index staan er inmiddels op.

**Wat er nog te beslissen valt** is één ding: de retrieval-kernel op
`feat/vector-chunk-lagen`. Die branch wholesale mergen is géén formaliteit — hij
loopt 63 commits achter en `ocd-api/main.py` verschilt ~900 regels, dus dat is
een integratie met conflicten in het hart van de API. De route is de kernel als
losse feature op main zetten (vier bestanden + tooling), reviewen, testen, en
dán beide branches opruimen.

**Randvoorwaarde**: eerst vaststellen vanaf welke commit Railway de `ocd-api`
deployt. Draait prod de kernel, dan is main niet zonder meer deploybaar; draait
prod main, dan is die branch afgeschreven werk. Dat is niet vanaf de
buitenkant te zien — de kernel is interne machinerie, geen endpoint — dus het
is een dashboard- of `railway status`-check.

---

## 4. Afbreken & herstel

Een gekilde run laat rommel achter die het dashboard misleidt:

```sql
-- 1. de openstaande fase afsluiten (constraint: running|ok|deels|gefaald)
UPDATE core.load_run SET status='gefaald', finished_at=now()
 WHERE status='running';
-- 2. de sync-run zelf afsluiten, herkenbaar
UPDATE audit.sync_run SET klaar_op=now(), opmerking='afgebroken tijdens <fase>'
 WHERE run_id=<id> AND klaar_op IS NULL;
```

Herstarten is veilig: alle fasen zijn idempotent (skip-guard op p2p,
`ON CONFLICT` op i2a, watermark op vth, resumable embed).

**Een afgekapte MV-refresh laat een spook-backend achter.** `subprocess.run`
kilt de client, maar de Postgres-backend merkt dat pas bij zijn volgende
netwerkactie en rekent rustig door — aan werk dat gegarandeerd verloren gaat,
want de `COMMIT` komt nooit. Ondertussen kost hij IO en houdt hij de lock vast.
Opruimen vóór je opnieuw begint:

```sql
SELECT pid, now()-query_start AS duur, left(query,60)
  FROM pg_stat_activity
 WHERE state='active' AND pid <> pg_backend_pid();
SELECT pg_terminate_backend(<pid>);
```

Let op de `pid <> pg_backend_pid()`: zoek je op de querytekst, dan matcht je
eigen query zichzelf en beëindig je je eigen sessie (gebeurd op 2026-08-01).

**Hervat in de volgorde van het runbook, niet vanaf het punt van de fout.**
Een mislukte rekenstap gaat achteraan: eerst de resterende harvest- en
verplaatsstappen (4, 5), dan verificatie (7), en pas daarna het dure rekenwerk
opnieuw. Dezelfde regel als in de sync zelf — bronnen eerst, rekenen later.

Noodroute als prod onherstelbaar afwijkt: `restore-dev-naar-prod.ps1`
(destructief, uren) — zie ook `prod-deploy-recovery-runbook.md`.

---

## 5. Wat dit runbook (nog) niet oplost

| # | Beperking | Gevolg |
|---|---|---|
| G-98 | *opgelost 2026-08-01* — delta brak af op een ongesorteerde lijst | 16 regelingen waren stil gemist; nu gefixt + regressietests |
| G-91 | verdwenen regelingen worden gedetecteerd maar niet opgevolgd | 11 vigerende regelingen in de DB die de DSO niet meer toont |
| G-97 | vectorlaag herbouwt volledig en ruimt niets op | `chunk_annotatie` + `chunk_categorie` worden elke run overgedaan; chunks van verdrongen expressies blijven staan en worden pas bij het lezen weggezeefd (45% bij `gm0796`) |
| G-94 | geen delta voor i2a/vth op prod; geen scheduling | stap 4 en 5 blijven handwerk |
| — | `repliceer_p2p_naar_prod.py` dekt p2p, maar de afgeleide herbouw (subdiv, MV's) is nog losse handmatige stappen | stap 3 is één script plus drie commando's; automatiseren kan zodra de volgorde zich bewezen heeft |
| — | de replicatie **verwijdert** niets op prod | een rij die lokaal is opgeruimd blijft daar staan; net als G-91 een bewuste keuze, geen automatisme |
| — | i2a-datum hardgecodeerd op `10-04-2026` | i2a laadt de april-toestand |
| — | **i2a per-bestuursorgaan-kanaal dood sinds de initial commit** (2026-04-12): bronhoudercode gaat mét prefix naar de RTR (`gm0363`), die de kale code verwacht (`0363`) | 342 calls per sync die per definitie 0 opleveren; geen OIN → STTR stilzwijgend overgeslagen; het rapport meldde intussen "343/343 ok" |
| — | rapportage meet exceptions, geen volledigheid | "0 fouten" gaf jarenlang valse geruststelling |
| — | doorwerkingsmeting (6b) hangt niet aan de pipeline, vereist lokale GPU | instructieregels.nl toont oordelen van vóór de sync tot iemand 6b draait; sinds 05-08 gaat de site tenminste niet meer stil de deur uit (pre-flight in `publish.py`) |
| — | verschoven top-K is niet detecteerbaar zonder te herscreenen | `stand.py` kan "nieuw artikel valt nu in de top-K van een oude regel" niet zien; vandaar de vuistregel in 6b |
| — | *opgelost 2026-08-01* — drieslag kostte ~1,5 u door een ongescopete naam-match | nu 5,5 min lokaal / 11 min prod; zie hieronder |

### Refresh-modus van de MV's

Sinds 2026-08-01 draait `refresh_drieslag.py` standaard een **gewone** REFRESH.
Die zet de view kort op slot; queries erop wachten. Dat is 's nachts akkoord
(gebruikerskeuze) en scheelt fors, want `CONCURRENTLY` bouwt een volledige
tweede kopie en verschilt die daarna. Gemeten op prod: `naammatch_signaal`
(2,1 GB / 6,2M rijen) liep met `CONCURRENTLY` ruim twee uur en werd IO-gebonden
— de dubbele kopie past niet in de 2 GB-geheugencap van de Railway-container.

Draai je overdag, terwijl viewer/bot moeten doorlezen: `--concurrently`.

### De echte oplossing: intra-scoping (uitgevoerd 2026-08-01)

De refresh was niet traag omdat hij veel moest doen, maar omdat hij het
**verkeerde** deed. `naammatch_signaal` vergeleek élke tekst in Nederland met
élke objectnaam in Nederland (6,3M treffers), waarna de volgende laag daar 99,3%
van weggooide en 43.045 overhield — de treffers binnen dezelfde regeling.

De intra-gescopete definitie stond al in
`scripts/2026-05-add-naammatch-signaal.sql` (gebruiker-keuze 2026-05-08) maar was
nooit toegepast; de database draaide nog v1. Migratie:
`scripts/2026-08-naammatch-intra-scoping.py`.

| | Vóór | Na |
|---|---|---|
| Rijen in de basis | 6.324.956 | **43.045** |
| Uitkomst downstream | 43.045 / 496.931 | **identiek** |
| Hele drieslag-fase lokaal | ~82 min | **5,5 min** |
| Op prod | haalde de 3-uurs-timeout niet | **11,1 min** |

De landelijke kruisvergelijking is niet verdwenen maar on-demand geworden:
`scripts/analyse-naammatch-cross-regeling.sql`. Niets in de codebase las haar
(geverifieerd).

Daarmee is incrementeel verversen niet meer nodig: een volledige herbouw van
5 minuten is prima, ook nachtelijks. Incrementeel maken bovenop een berekening
die 147× te veel deed, zou het echte probleem juist hebben verstopt.

**De laatste is de belangrijkste openstaande verbetering.** Het rapport zou
*verwacht* (preview) naast *daadwerkelijk geladen* moeten zetten en afwijkingen
markeren. Dan was G-98 in juni opgevallen in plaats van in augustus, en dan
meet ook de `publish.py`-poort iets zinnigs.

---

## 6. Cadans

| Wat | Frequentie | Hoe |
|---|---|---|
| Volledige sync (stap 0–4, 7–9) | wekelijks | dit runbook |
| Embeddings + onderwerp-as (stap 6) | elke sync | draait standaard mee in `full_sync.py` |
| Onderwerp-as naar prod | na elke stap 6 die lokaal draaide | `2026-08-06-categorie-naar-productie.py --ja` (2 s) |
| Doorwerkingsmeting (stap 6b) | na elke sync die omgevingsplannen of instructieregels raakte | lokaal, ná stap 6; `match/stand.py` zegt of het moet |
| `diff_dso_bronhouder_coverage.py` | maandelijks | zwaardere coverage-diff naast de preview |
| Wro/IMRO2006 (`load-wro-imro2006`) | los, ~24 min | landelijke PDOK-herparse, niet in de sync |
| MER (`load-mer`) | los, seconden | aparte harvester-repo |
| `core.gemeentegrens` | 1×/jaar | gemeente-herindelingen |
| Prune verouderde versies | op indicatie | dry-run eerst |

---

## Bijlage — Run 2026-08-01 (uitgevoerd)

### Preview vooraf (stap 0), ná de G-98-fix

```
p2p    TE LADEN: 11 nieuw + 5 nieuwe versie = 16   (prod mist er 15)
       VERDRONGEN: 5     VERDWENEN: 11
vth    TE LADEN: 2 open dagen, samen 1.256 kennisgevingen
embed  TE EMBEDDEN: 178.111 tekst_elementen zonder embedding
```

De 16 liepen terug tot 25 juni; 8 daarvan waren **ouder dan de watermark**
(22 juli). Deze run is daarom met `--sinds 2026-06-01T00:00:00Z` gedraaid —
precies het geval waarvoor stap 1 die waarschuwing bevat. Met de standaard
watermark waren er 8 van de 16 blijven liggen.

### Uitkomst stap 1 (lokaal, 3,2 uur)

| | Verwacht (preview) | Geladen | |
|---|---|---|---|
| p2p-regelingen | 16 | **16** | ✅ 1974 → 1990 |
| vth-kennisgevingen | 1.256 | 1.256 | ✅ |

**De preview-vs-uitkomst-check klopt.** Dat is de controle die tot vandaag
ontbrak en die G-98 vier maanden lang had kunnen vangen.

Fase-timings: p2p+i2a 58 min · vth-load 1 min · enrich 21 min ·
geometrie-backfill 9 min · drieslag-MV's **82 min** (de lange pool, met
`naammatch_signaal` als zwaarste) · health-MV's ~17 min.

### Twee 503's — en wat ze blootlegden

| Fase | Wat | Gevolg |
|---|---|---|
| i2a | 503 op gemeente Nieuwegein (gm0356) | 342/343 gemeenten ok |
| vth | 503 op pagina 62/78 van de BOPA-snapshot | run afgebroken, ~3.000 records niet verwerkt |

Beide waren transiënte hikken, géén storing. Ze legden bloot dat de
Presenteren-calls **geen retry** hadden terwijl dit document beweerde van wel.
Gefixt tijdens de run (`src/http_retry.py`); daarna beide stappen herhaald en
geslaagd — BOPA compleet op 78/78 (15.494 records), i2a-Nieuwegein ok.

### Stap 2

De loader had de 5 verdrongen expressies zelf al gemarkeerd
(`markeer_siblings_inactief` in `api_loader`); inactief ging 8 → 13. De
autoritatieve controle bevestigde dat: 0 extra te markeren, 5 vigerende
expressies via DSO bevestigd. Stap 2 blijft in het runbook staan als
verificatie — je wilt weten dát het klopt, niet aannemen dat het klopt.
