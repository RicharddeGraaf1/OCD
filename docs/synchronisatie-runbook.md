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
bronhouder mét nieuwe regelingen), i2a ~20 min, vth ~15 min, post (drieslag-MV's)
de lange pool.

> **De i2a-duur is op 2026-08-08 twee keer veranderd.** Hij stond op "~3 min",
> maar dat was de tijd van een fase die niets deed (G-117: de RTR kreeg een
> geprefixte bronhoudercode en gaf altijd 0 terug). Na die fix werd het **5,6
> uur**, want toen werden er voor het eerst ~50.000 DMN-bestanden opgehaald.
> Met de delta hieronder is het ~20 min. Zie §i2a-delta.

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

### i2a-delta — hoe de fase van 5,6 uur naar ~20 min ging

*Toegevoegd 2026-08-08. Relevant voor stap 1 (de i2a-fase) en voor de cadans in
§6.*

**Waar de kosten zitten.** Niet in de lijsten maar in de DMN-bestanden. De
loader haalt per regelbestand een XML op (`GET /toepasbareRegels/{id}/sttrBestand`)
en parseert die; bij ~150 bestanden per bronhouder en 343 bronhouders zijn dat
ongeveer **50.000 downloads**. De lijst-calls zijn er ~1.000 en vallen weg.

**Waar de delta op rust.** De STTR-lijst levert per bestand een
`laatsteWijzigingDatum` op secondeniveau. Die staat nu in
`i2a.toepasbaar_regelbestand.laatste_wijziging`. Is hij bij een volgende run
gelijk, dan is de inhoud ongewijzigd en slaat de loader de XML over.

| | |
|---|---|
| gemeten op gm1699 (148 regelbestanden) | ronde 1: **52,3 s** · ronde 2: **3,1 s** |
| geëxtrapoleerd over 343 bronhouders | 5,6 uur → **~20 min** |

**Drie eigenschappen om te kennen voordat je erop vertrouwt:**

1. De datum wordt **pas vastgelegd ná een geslaagde verwerking**. Een
   afgebroken run laat dus geen bestand achter dat ten onrechte als "bij" geldt
   — precies het type stil gat dat deze sync vier keer opleverde.
2. Het is **geen watermark over alle bestanden heen** maar een vergelijking per
   bestand. Bewust: bij p2p bleek een watermark op registratietijdstip lek,
   omdat items later in de lijst kunnen verschijnen met een ouder tijdstip.
3. **Verdwenen regelbestanden worden niet opgeruimd.** Staat een bestand niet
   meer in de lijst, dan blijft de rij staan — dezelfde keuze als G-91 bij p2p:
   verdwijnen uit een lijst is geen bewijs van intrekking.

### i2a-peildatum — van 10 april naar vandaag

*Toegevoegd 2026-08-09. Hoort onlosmakelijk bij de delta hierboven.*

RTR en STTR zijn **geldigheidsgestuurd**: de `datum`-parameter bepaalt welke
toestand je terugkrijgt. Die stond op drie plekken hardgecodeerd op
`10-04-2026`, en is nu `_peildatum()` — standaard vandaag, te overschrijven met
`IMTR_PEILDATUM` in `.env` als je een oude toestand wilt reproduceren.

Wat dat scheelde, gemeten over 19 bronhouders (3.128 regelbestanden), april
tegenover vandaag:

| | |
|---|---|
| nieuwe regelbestanden | 2 |
| verdwenen regelbestanden | 2 |
| **nieuwere `laatsteWijzigingDatum`** | **52 (~1,7%)** |
| Amsterdam specifiek | 161 → 166 regelbestanden |

Dus: geen instorting, maar ~1,7% van de inhoud stond vier maanden stil, en
nieuw werk van bronhouders kwam per definitie niet binnen.

**Let op bij het zelf nameten**: `page.totalElements` in de RTR is *niet* het
aantal items. Amsterdam levert 120 activiteiten terwijl het veld 110 (april) of
113 (vandaag) zegt; gm1699 levert er 100 bij een gemelde 90. Tel de items, niet
het veld — `preview_sync --i2a` doet dat laatste (pageSize 1) en rapporteert
daardoor structureel ~8% te laag.

**De twee mechanismen in de juiste volgorde.** De peildatum bepaalt *wat er
gevraagd wordt*, de delta bepaalt *wat daarvan opnieuw wordt opgehaald*. Ze zijn
elkaars voorwaarde: zonder delta zou het bijwerken van de peildatum de fase weer
5,6 uur maken; zonder peildatum-fix zou de delta een steeds sneller antwoord op
een steeds oudere vraag geven.

**Het watermerk moest eerst worden ingehaald.** `laatste_wijziging` was maar
voor 148 van de 59.646 bestanden gevuld (de gm1699-inhaalronde); voor de rest
`NULL`, dus de eerstvolgende i2a-run zou hoe dan ook ~5,6 uur duren. Daarom op
09-08 `scripts/backfill_i2a_watermerk.py` gedraaid:

- **Eén kolom in één tabel** — `i2a.toepasbaar_regelbestand.laatste_wijziging`.
  Geen regelbestand, geen `dmn_element`, geen `uitvoeringsregel`; de inhoud
  blijft ongemoeid.
- **Waarde komt van de STTR op peildatum `10-04-2026`**, de datum waarop die
  inhoud is geladen. We leggen dus vast wat er wérkelijk in de database staat,
  niet wat vandaag geldt — waardoor de eerstvolgende run (peildatum vandaag)
  precies de sindsdien gewijzigde bestanden ophaalt.
- **Twee guards**: alleen bestanden met aantoonbare inhoud in `dmn_element`
  **of** `uitvoeringsregel` (59.450 van de 59.498; de 48 zonder inhoud blijven
  leeg en worden dus opgehaald), en alleen waar het watermerk nu leeg is.

Meet die dekking over **beide** tabellen. Alleen `dmn_element` tellen geeft
22.599 schijnbaar lege bestanden: *Maatregelen*-bestanden bevatten per ontwerp
geen `<semantic:decision>`, alleen uitvoeringsregels.

### Stap 5 — i2a naar prod (afweging)

i2a heeft sinds 2026-08-08 wél een delta (zie hierboven), maar nog steeds geen
push-script. Vergelijk na stap 1 de tellingen
(`i2a.toepasbaar_regelbestand`, `i2a.uitvoeringsregel`) tussen lokaal en prod:

- **verschil triviaal** → laten staan tot de volgende gelegenheid;
- **verschil substantieel** → repliceren volgens hetzelfde principe als stap 3
  (rijen van lokaal naar prod). De i2a-tabellen hangen niet aan `frbr_expression`
  maar aan `functionele_structuur_ref` / OIN, dus de filterset is een andere —
  de route is identiek. **Niet** de loader tegen prod draaien.

Twee dingen om te weten vóór je hier tijd in steekt:

- **Lokaal staat sinds 2026-08-09 op peildatum vandaag, prod op de april-stand.**
  Het verschil tussen beide is dus deels inhoudelijk (de +46% hieronder) en
  deels een peildatum-verschil van vier maanden. Repliceer je, dan neem je de
  actuele stand mee — dat is de bedoeling, maar weet dat het twee wijzigingen
  in één beweging zijn.
- **Het per-bestuursorgaan-kanaal lag sinds de initial commit stil** en is op
  2026-08-08 gerepareerd: de loader stuurde de bronhouder-code mét prefix
  (`gm0363`) terwijl de RTR de kale code verwacht (`0363` → 113 activiteiten).
  Geen activiteiten → geen OIN → STTR stilzwijgend overgeslagen. Na de fix:
  **+384.178 uitvoeringsregels (+46%)**, van 831.835 naar 1.216.013.
- **Productie draait bewust nog op de oude stand** (831.835). Het verschil met
  lokaal is dus geen datagat maar een openstaande keuze: eerst moet vaststaan
  welke afnemer de toepasbare regels op prod gebruikt. Vastgelegd in de vault
  onder Gebruikersinput 2026-08-08. Wie hier een afwijking ziet — of de
  regressiecheck die hem meldt — weet nu waarom.

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

#### Sinds 2026-08-08: `refresh-v2a` in plaats van de volledige scan

```bash
python -m src.cli refresh-v2a                    # droogloop: toon de dirty-set
python -m src.cli refresh-v2a --ja --opruimen
```

Dit is de executor uit het G-97-ontwerp, waarvan tot nu toe alleen fase 1
(`v2a.embed_state`) bestond. Hij bepaalt met één query welke regelingen nieuw
of gewijzigd zijn — content-hash, geen timestamp, want p2p-herlaad is
UPSERT-DO-NOTHING — en embedt alleen die. Gemeten na de sync van 2026-08-07:
**26 dirty van 1.978**, tegenover de 1.979 die `run_overnight.py` fase 4a
allemaal langsliep.

`--opruimen` verwijdert daarnaast de chunks van verdrongen expressies, de
tweede helft van G-97. Let op de scope: `v2a.tekst_embedding` bevat óók
`source_type='wro'` (39.358 expressies) en `'ontwerp'` (~240). Een naïeve
"bestaat niet meer in p2p.regeling"-query markeert die als wees en zou de halve
index wissen — de droogloop gaf 39.846 waar er 12 echt waren. De scherpe
definitie is: staat wél in `p2p.regeling`, maar op `inactief`.

`run_overnight.py` blijft bestaan voor een volledige herbouw, maar hoort niet
meer in een gewone sync.

#### Overslaan mag, maar weet wat je overslaat

```bash
python scripts/full_sync.py --skip-embed     # sync zonder de vectorlaag
python scripts/run_overnight.py              # volledige herbouw, uren
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

- `SYNC-REPORT-<datum>.md` — kijk éérst naar de sectie **Regressiecheck**. Die
  vergelijkt niet of een fase gedraaid heeft maar of hij iets *deed*, op basis
  van `details.totaal_voor`/`totaal_na` per `core.load_run`. Drie signalen:

  | Signaal | Betekenis |
  |---|---|
  | totaal **daalde** | er is data verdwenen tijdens een fase die "ok" meldt |
  | preview verwachtte +N, geladen +M (M < N) | stille onvolledigheid bínnen deze run |
  | 3 runs op rij geen aangroei | de bron staat stil — dit is het i2a-geval (G-117) |

  Losse controle op een eerdere run: `python -m src.sync_regressie --run-id <n>`.
  Retroactief getest: de check vindt zowel G-98 (juli, `ozon-regelingen` drie
  runs stil) als G-117 (`rtr-toepasbare-regels` stil op 63.792).

  De verwachting komt uit de preview en wordt aan het begin van de run in
  `audit.sync_run.metrics->'verwacht'` gezet. Blijft die leeg (DSO onbereikbaar),
  dan valt de check terug op de historie.
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

### Stap 10 — Wijzigingsspoor opruimen (periodiek, niet elke sync)

*Toegevoegd 2026-08-09 na de eerste uitvoering. Achtergrond: vault `gaps.md`
G-123.*

`ontwerp_loader` beslist **bij binnenkomst** of een ontwerp of besluitversie
relevant is. Niets past die toets later opnieuw toe, dus rijen komen binnen
onder een voorwaarde en vertrekken niet als die vervalt. Een besluitversie die
in werking is getreden, of een ontwerp dat ouder is dan onze vigerende versie,
blijft met zijn hele gevolg staan.

```bash
python scripts/ruim_wijzigingsspoor_op.py               # droogloop
python scripts/ruim_wijzigingsspoor_op.py --uitvoeren   # vangnet + verwijderen
```

**Het criterium is de intake-logica van de loader zelf**, niet een eigen regel.
Dat is geen esthetiek: verwijder je iets wat de loader morgen weer binnenlaat,
dan koop je churn — weggooien, opnieuw downloaden, weggooien. Door precies te
spiegelen wat `load_ontwerp` en `load_besluitversie` vandaag zouden doen, is het
resultaat stabiel.

**Uitgevoerd 2026-08-09** — 225 van de 445 besluiten (119 ontwerpen, 106
besluitversies):

| tabel | verwijderd | van | aandeel |
|---|---|---|---|
| `annotatie_delta` | 320.529 | 721.253 | 44% |
| `locatie_delta` | 318.195 | 1.588.950 | 20% |
| `tekst_element` | 305.877 | 725.428 | 42% |
| `procedurestap` | 2.028 | 6.027 | 34% |
| `juridische_regel_*_delta` | 3.231 | 95.895 | 3% |

Vier dingen die deze stap bewust **niet** doet, elk met een reden:

1. **De rij in `p2pwijziging.besluit` blijft staan** (alle 445). Die is de enige
   bron van inwerkingtredingsdatum die we hebben (zie G-121 → G-108: 98 van de
   124 besluitversies matchen op een vigerende regeling) én de enige rem op
   herladen — zonder die rij is er niets dat zegt "hier is al over besloten".
2. **De OW-objecten blijven staan.** `p2p.activiteit`, `locatie`, `norm` en
   `gebiedsaanwijzing` hebben geen regeling-kolom; het zijn IMOW-objecten op
   `identificatie`, gedeeld over regelingen. Gemeten op de inactieve regelingen:
   0 van de 18 activiteiten en 0 van de 22 locaties was exclusief. Wie op
   OW-objectniveau wil opruimen, ruimt per definitie bijna nooit iets op.
3. **De vectorlaag blijft ongemoeid.** Van de 469.666 chunks met
   `source_type='ontwerp'` hoort er 0 bij de opgeruimde set. Er staan wél 70.376
   chunks op deze expressies, maar dat zijn gewone vigerende artikelen van de 98
   expressies die inmiddels vigeren.
4. **Niets weghalen wat de volgende sync terugbrengt.** Het criterium is één
   functie — `ontwerp_loader._is_relevant` — die zowel de loader als dit script
   gebruikt. Loopt dat uiteen, dan krijg je churn: weggooien, opnieuw
   downloaden, weggooien.

   Dat is op 09-08 ook de reden geweest om de *loader* aan te scherpen in plaats
   van het script. Er stonden 39 ontwerpen op een basis-expressie die niet meer
   vigeerde, terwijl de loader ze wél opnieuw binnenliet — hij toetste of een
   ontwerp *jonger is dan* onze vigerende versie, niet of het erop *voortbouwt*.
   Nu eist voorwaarde 4 dat `wijzigt_expression` de vigerende expressie is
   (een ontwerp kan in deze keten niet op een oudere consolidatie zitten).
   Tweede ronde daarna: 684.519 locatie_delta (54%), 79.451 tekst_element,
   68.513 annotatie_delta.

**Vangnet.** `--uitvoeren` kopieert eerst alles naar schema `vangnet`
(prefix `w<datum>_`, 660 MB op 09-08). Dat is geen overdreven voorzichtigheid:
het verwijdercriterium is juist dat de loader deze rijen niet meer binnenlaat,
dus **een herstelrun bestaat niet**. Ruim het vangnet pas op als de eerstvolgende
sync schoon is verlopen: `DROP SCHEMA vangnet CASCADE;`

**Ruimte.** `VACUUM` geeft de ruimte terug aan de tabel, niet aan de schijf; de
bestandsgroottes blijven dus staan tot nieuwe rijen ze hervullen. Op 09-08 pakte
autovacuum de tabellen zelf op. Draai je 'm handmatig, zet dan eerst
`max_parallel_maintenance_workers = 0` — anders loopt hij lokaal stuk op
`could not resize shared memory segment` (Docker `/dev/shm`).

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
| G-94 | *deels opgelost 2026-08-08* — i2a heeft nu een delta op `laatsteWijzigingDatum` (5,6 u → ~20 min). vth heeft er nog geen; scheduling ontbreekt nog steeds | stap 4 en 5 blijven handwerk |
| — | `repliceer_p2p_naar_prod.py` dekt p2p, maar de afgeleide herbouw (subdiv, MV's) is nog losse handmatige stappen | stap 3 is één script plus drie commando's; automatiseren kan zodra de volgorde zich bewezen heeft |
| — | de replicatie **verwijdert** niets op prod | een rij die lokaal is opgeruimd blijft daar staan; net als G-91 een bewuste keuze, geen automatisme. **Let op sinds 09-08**: stap 10 heeft lokaal 947.860 rijen uit `p2pwijziging` gehaald die op prod nog staan — dat verschil is bedoeld, niet een gat |
| — | *opgelost 2026-08-09* — de twee koppelingen van een tekstdeel werden nooit geschreven | `tekstdeel_hoofdlijn` stond landelijk op **0 rijen** en 40% van de gebiedsaanwijzingen hing nergens aan. De API levert `hoofdlijnRefs`/`gebiedsaanwijzingRefs`; `load_divisieannotaties` las ze niet. Na fix + backfill: 4.955 en 6.965 koppelingen; wezen van 410→7 (hoofdlijn) en 1.942→196 (gebiedsaanwijzing). Zie vault G-124 |
| — | de relevantietoets van `ontwerp_loader` wordt alleen bij intake toegepast | rijen komen binnen onder een voorwaarde en vertrekken niet als die vervalt; stap 10 ruimt op, maar de loader blijft het opnieuw opbouwen. Structureel zou de toets bij elke run over de bestaande voorraad moeten (vault G-123) |
| — | *opgelost 2026-08-09* — i2a-datum stond hardgecodeerd op `10-04-2026` | nu `_peildatum()` = vandaag. Gemeten effect over 19 bronhouders: 2 nieuw, 2 weg, 52 van 3.128 met nieuwere inhoud (~1,7%). De eerstvolgende run duurt nog ~5,6 u omdat `laatste_wijziging` nog vrijwel overal `NULL` is |
| — | de preview stuurde de RTR een geprefixte code (`gm0344`) | *opgelost 2026-08-09* — dezelfde G-117-fout als in de loader, waardoor élke gemeente 0 activiteiten leek te hebben. Preview hergebruikt nu de loader-helpers in plaats van ze over te schrijven |
| — | *opgelost 2026-08-08* — i2a-kanaal lag sinds de initial commit stil (geprefixte bronhoudercode) | +384.178 uitvoeringsregels (+46%) na de fix; retry op 503 toegevoegd, want die ontbrak ook |
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
| i2a (in de sync) | elke sync, ~20 min | kan sinds de delta van 2026-08-08 gewoon meedraaien; vóór die tijd was de keuze "3 min omdat hij niets deed" of "5,6 uur" |
| Embeddings + onderwerp-as (stap 6) | elke sync | draait standaard mee in `full_sync.py` |
| Onderwerp-as naar prod | na elke stap 6 die lokaal draaide | `2026-08-06-categorie-naar-productie.py --ja` (2 s) |
| Doorwerkingsmeting (stap 6b) | na elke sync die omgevingsplannen of instructieregels raakte | lokaal, ná stap 6; `match/stand.py` zegt of het moet |
| `diff_dso_bronhouder_coverage.py` | maandelijks | zwaardere coverage-diff naast de preview |
| Wro/IMRO2006 (`load-wro-imro2006`) | los, ~24 min | landelijke PDOK-herparse, niet in de sync |
| MER (`load-mer`) | los, seconden | aparte harvester-repo |
| `core.gemeentegrens` | 1×/jaar | gemeente-herindelingen |
| Prune verouderde versies | op indicatie | dry-run eerst |
| Wijzigingsspoor opruimen (stap 10) | maandelijks, of als `p2pwijziging` hard groeit | `ruim_wijzigingsspoor_op.py`; droogloop eerst, vangnet daarna opruimen |

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
