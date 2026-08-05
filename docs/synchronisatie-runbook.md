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
2. **Prod-directe delta, geen dump/restore.** De 80 GB `restore-dev-naar-prod`
   is de noodroute, niet de route (G-94).
3. **Fasen zijn losgekoppeld.** Een mislukte deploy mag data nooit raken; een
   mislukte i2a mag p2p niet blokkeren. Elke fase heeft zijn eigen `load_run`.
4. **Zuinig tegen de DSO.** Rate-limiter 50/s; nooit onbegrensd pagineren;
   skip-guard vóór laden. Een volledige lijst-sweep is ~10 calls — goedkoper
   dan de 381 van de per-bronhouder-sweep.
5. **Nooit prunen of publiceren tijdens een load.**

## 2. Rolverdeling van de twee databases

| | LOKAAL (Docker, `localhost:5434/dso`) | PROD (Railway PostGIS) |
|---|---|---|
| Rol | werkbank: evals, viewer-tests, analyses, zware herbouw | wat eindgebruikers zien |
| Consumenten | jij | ocd-api → viewer, bot, ponsenkaart, instructieregels, vergunningenregister |
| Bereikbaar | altijd | alleen met de **TCP-proxy tijdelijk aan** (dashboard → PostGIS → Settings → Networking) |
| Parallelisme | normaal | `get_conn()` zet het uit (kleine `/dev/shm`) |

Beide moeten actueel zijn. Lokaal achterlaten betekent dat je evals op stale
data draait; prod achterlaten betekent dat de sites stale zijn.

---

## 3. De volgorde

```
0. PREVIEW          read-only, beide DB's            ~1 min
1. LOKAAL laden     p2p + i2a + vth + post           ~1–3 u
2. NABEWERKING      verdrongen versies markeren      ~1 min
3. PROD delta       p2p + post                       ~2–3 u
4. VTH → prod       delta-push                       ~10 min
5. I2A → prod       afweging, meestal overslaan      —
6. EMBEDDINGS       apart en bewust                  uren
6b. DOORWERKING     instructieregels.nl-meting       uren, lokale GPU
7. VERIFICATIE      beide DB's + API                 ~5 min
8. DOWNSTREAM       gebakken sites herbouwen         ~15 min
9. NAZORG           proxy dicht, VACUUM, loggen,
                    code van de run committen op main ~15 min
```

Stappen 0–4 en 7–9 horen bij elke sync. Stap 5 en 6 zijn afwegingen. Stap 6b
hangt aan 6: hij zoekt in de vectorindex, dus zonder embeddings meet hij tegen
een index die de nieuwe regelingen niet kent.

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

**Let op `--sinds`.** De watermark is "start vorige geslaagde sync − 2 dagen".
Als de preview achterstand toont die **ouder** is dan die watermark (bv. omdat
een eerdere run stilletjes te weinig laadde), pak dan expliciet een ruimer
venster:

```bash
python scripts/full_sync.py --label "sync-<datum>" --skip-embed \
  --sinds 2026-06-01T00:00:00Z
```

Dat is goedkoop: de lijst-sweep kost dezelfde ~10 calls, de regelingen zijn al
opgehaald vóór het laden, en de skip-guard herkent al-geladen expressies in
~1 ms. `--full-p2p` heb je hiervoor **niet** nodig; dat is de per-bronhouder-
sweep en alleen bedoeld na een verse restore.

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

### Stap 3 — Prod-delta

Vereist de TCP-proxy aan en `PROD_DB_URL` in `.env`.

```bash
python scripts/preview_sync.py --target prod          # eerst kijken
python scripts/full_sync.py --target prod --skip-i2a --skip-vth --skip-embed \
  --label "prod-delta-<datum>"                        # typ 'PROD' ter bevestiging
```

`--skip-i2a --skip-vth` is hier **niet optioneel**: beide hebben geen delta en
pollen alle bronhouders — over de proxy onwerkbaar traag. Prod's
`vth.etl_run`-watermark loopt bovendien achter op de vth-data (die komt via de
push van stap 4), dus een vth-fase tegen prod zou dagen opnieuw ophalen.

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
- **verschil substantieel** → `full_sync.py --target prod --skip-p2p
  --skip-vth --skip-post --skip-embed` (pollt alle 342 gemeenten over de proxy;
  reken op traag).

Bedenk daarbij: de i2a-loader bevraagt de RTR/STTR met een **hardgecodeerde
`datum: "10-04-2026"`**. Zolang die er staat, haalt i2a per definitie de
toestand van 10 april op — een verschil dat níét verschijnt is dus geen bewijs
van actualiteit.

### Stap 6 — Embeddings (apart en bewust)

Niet meenemen in de sync. Twee redenen:

1. `run_overnight.py` sluit af met **`git checkout feat/vector-chunk-lagen` +
   commit** op `c:/GIT/OCD`. Met een dirty working tree op een andere branch is
   dat een ongeluk-in-wording. Commit of stash je WIP eerst.
2. De embed-stap zelf is resumable en goedkoop, maar dezelfde run herbouwt
   `chunk_annotatie` en `chunk_categorie` **volledig** (uren), ook als er weinig
   nieuw is (G-97).

```bash
python scripts/run_overnight.py                    # lokaal
OCD_DB_URL=<prod-dsn> python scripts/run_overnight.py   # daarna prod
```

Tot G-97 is opgelost geldt: **nieuwe regelingen zijn pas semantisch vindbaar na
deze stap.** Sla je hem over, dan is de sync wél compleet en de vindlaag niet.

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
| G-97 | vectorindex hangt niet aan de pipeline | nieuwe regelingen niet semantisch vindbaar tot stap 6 |
| G-94 | geen delta voor i2a/vth op prod; geen scheduling | stap 4 en 5 blijven handwerk |
| — | i2a-datum hardgecodeerd op `10-04-2026` | i2a laadt de april-toestand |
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
| Embeddings (stap 6) | na elke sync met noemenswaardig nieuw | apart, bewust |
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
