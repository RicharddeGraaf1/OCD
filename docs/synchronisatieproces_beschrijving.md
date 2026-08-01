# Synchronisatieproces OCD — beschrijving, timings & efficiëntie

*Laatst bijgewerkt: 2026-08-01*

Dit document beschrijft hoe de nachtelijke synchronisatie van de OCD-database
werkt, wat elke fase doet, **hoe lang elke stap duurt**, en — kritisch — **hoe
we de DSO-API zuinig bevragen** zodat de API-key niet geblokkeerd raakt.

> **Ga je daadwerkelijk syncen?** Volg dan
> [synchronisatie-runbook.md](synchronisatie-runbook.md) — dat is het draaiboek
> (volgorde, go/no-go, prod, downstream, nazorg). Dit document is de referentie
> eronder.

```bash
cd c:/GIT/OCD/dso-loader
python scripts/full_sync.py --preview            # eerst kijken (schrijft niets)
python scripts/full_sync.py --label "sync-<datum>"
```

> **`full_sync` laadt niets "vol".** De naam slaat op de orkestratie (alle
> bronnen in één run), niet op de omvang. Elke fase is incrementeel; alleen
> `--full-p2p` haalt bewust alles opnieuw op.

| Vlag | Effect |
|---|---|
| `--full-p2p` | volledige per-bronhouder-sweep i.p.v. de registratietijdstip-delta (alleen voor verse restore / integriteitscheck) |
| `--sinds <ISO-UTC>` | ondergrens voor de p2p-delta forceren (default = start vorige geslaagde sync − 2 dagen) |
| `--skip-p2p` / `--skip-i2a` / `--skip-vth` / `--skip-post` / `--skip-embed` | fase overslaan |
| `--target local` (default) `\| prod` | DB-doelwit; `prod` draait de sync **direct tegen de Railway-prod-DB** (`PROD_DB_URL` uit `.env`, via de TCP-proxy) |
| `--dsn <connectstring>` | expliciete doel-DB; overschrijft `--target` |
| `--yes` | sla de prod-typbevestiging over (voor cron/non-interactief) |
| `--preview` | READ-ONLY: toon per bron wát er geladen zou worden, en stop |

---

## Eerst kijken, dan laden — `preview_sync.py`

**Draai altijd eerst een preview.** Een sync die "0 fouten" meldt zegt niets
over of hij het júíste heeft geladen; de delta-bug hieronder (G-98) leefde
maanden onder precies zo'n groene rapportage. De preview raakt de database niet
en bevraagt de bronnen alleen met lichte lijst-calls.

```bash
python scripts/preview_sync.py                    # lokale DB
python scripts/preview_sync.py --target prod      # tegen prod (read-only)
python scripts/preview_sync.py --vergelijk-prod   # toont er ook bij wat prod mist
python scripts/preview_sync.py --i2a              # inclusief de i2a-poll
python scripts/preview_sync.py --json             # machineleesbaar
python scripts/full_sync.py --preview             # zelfde, met de skip-vlaggen van de sync
```

| Bron | Preview-kost | Wat je te zien krijgt |
|---|---|---|
| p2p | ~10 lijst-calls | per regeling: **nieuw** / **nieuwe versie** / **verdrongen** / **verdwenen** |
| vth | 1 SRU-call per open dag | aantal kennisgevingen per openstaande dag + enrich-achterstand |
| i2a | ~342 calls (opt-in) | RTR-activiteiten per gemeente vs. wat in de DB zit |
| embed | alleen DB | tekst_elementen zonder embedding |

De preview kijkt **beide kanten op**: wat de DSO heeft en wij niet (te laden),
én wat wij vigerend hebben terwijl de DSO het niet meer toont. Die tweede groep
splitst hij in *verdrongen* (het work bestaat nog, er is een nieuwere expressie
→ na het laden `markeer_verouderde_expressies.py`) en *verdwenen* (het work is
weg → intrekking, G-91, wordt door de sync **niet** opgeruimd).

Standaard kijkt de p2p-preview naar de **volledige lijst**, niet vanaf de
watermark: dat kost dezelfde ~10 calls en laat ook achterstand zien die van
vóór de laatste sync dateert. Met `--sinds` beperk je alsnog het venster.

---

## Prod-directe delta-sync (i.p.v. de 80 GB dump/restore)

Sinds de goedkope registratietijdstip-delta hoeft een prod-verversing géén
volledige dump→restore meer te zijn. `full_sync.py --target prod` draait dezelfde
fasen **rechtstreeks tegen productie**, waarbij de delta alleen de bronhouders
met nieuwe registraties raakt.

**Aanbevolen (snel):**

```bash
# Vereist: Railway TCP-proxy tijdelijk AAN; PROD_DB_URL in dso-loader/.env.
cd c:/GIT/OCD/dso-loader
python scripts/full_sync.py --target prod --skip-i2a --skip-vth --label "prod-delta-<datum>"
```

Wat er dan tegen prod draait: preflight → snapshot/dedup (idempotent) →
**p2p-delta** (alleen nieuwe regelingen) → post (`regeling_load`-backfill,
repair-pons, ponsenkaart-stats, drieslag-MV's, health-MV's) → embeddings
(lokale Ollama, schrijft vectors direct in prod).

**Veiligheid & mechaniek:**

- Een prod-doelwit vraagt een **typbevestiging** (`PROD`) tenzij `--yes`.
- De connectstring wordt **gemaskeerd** in log/rapport (nooit wachtwoord in het
  logbestand).
- `get_conn()` zet bij een prod-DSN automatisch `max_parallel_workers*=0` — de
  Railway-container heeft een kleine `/dev/shm`, anders falen REFRESH/index-builds
  met *"could not resize shared memory segment"*.
- `sinds` komt uit prod's eigen `audit.sync_run` (na de restore = de dev-actualiteit),
  dus de delta pakt precies alles ná de laatste stand die op prod staat. Elke
  prod-run voegt zelf een `sync_run`-rij toe → volgende keer schuift `sinds` mee.

**Nog niet in de prod-delta (bewust overslaan met `--skip-i2a --skip-vth`):**
i2a en vth hebben nog geen delta en pollen álle bronhouders → over de proxy traag.
Laat die voorlopig via de lokale sync + restore lopen, of draai ze gericht. Delta
voor i2a/vth is de volgende stap (gaps G-94).

---

---

## TL;DR — de efficiënte manier

1. **p2p bevraagt de DSO zuinig.** Eén regeling-zoekopdracht per bronhouder
   (of, met de delta, één globale gesorteerde sweep). De rate-limiter staat op
   50 req/s; een volledige p2p-poll is ~400 calls ≈ 8 seconden API-tijd. Dit is
   **niet** de bottleneck en **niet** API-onvriendelijk.
2. **Zware DB-nabewerking alléén draaien als er iets veranderd is.** De
   `locatie_subdiv`-herbouw (ST_Subdivide, ~28s per bronhouder) mag nooit
   onvoorwaardelijk per bronhouder draaien — dat maakte een niks-nieuw-sync
   ~3 uur lang (zie hieronder). Nu voorwaardelijk: alleen bij daadwerkelijk
   geladen regelingen.
3. **De p2p-delta** slaat bronhouders zonder nieuwe registratie helemaal over,
   wat de API-calls én de DB-nabewerking tot een minimum terugbrengt.

---

## De regressie die p2p van minuten naar ~3 uur bracht

**Historisch was p2p enkele minuten; vth was de langere fase.** Dat klopt. De
~3 uur die een recente sync in p2p verbruikte was **geen API-kost en geen
regressie in het pollen** — het was een DB-nabewerking die per ongeluk
onvoorwaardelijk ging draaien.

### Wat er precies misging

In `load_via_api` (api_loader.py) staat na het laden van een bronhouder een
herbouw van de afgeleide tabel `locatie_subdiv` (subdivided geometrie, versnelt
geo-queries in de API). Die stond **buiten de per-regeling-lus en zonder
voorwaarde** — dus hij draaide voor **elke** bronhouder, óók als alle
regelingen werden overgeslagen (*Skip — al geladen*).

Toegevoegd in commit `c0a74be` ("loader: borg locatie_subdiv in pipeline + DDL").
Vóór die commit was er geen per-bronhouder-subdiv en was p2p minuten.

### De cijfers (gemeten 2026-07-24)

| | Waarde |
|---|---|
| Eén subdiv-herbouw (Groningen, 4.093 stukjes) | **17,9 s** |
| Eén subdiv-herbouw (Amsterdam, 65.194 stukjes) | **44,1 s** |
| Gemiddeld per bronhouder | ~28 s |
| Onvoorwaardelijk × 381 bronhouders | **≈ 3 uur** |
| Herberekende stukjes in één niks-nieuw-sweep | **2.039.988** |
| API-tijd voor dezelfde p2p-sweep (~400 calls @ 50/s) | ~8 s |
| Skip-check per regeling (geïndexeerd) | 1,1 ms |

Kortom: **≈ 3 uur DB-werk om geometrie te herbouwen die niet veranderd was**,
tegenover seconden echte API- en laad-tijd.

### De fix

`load_via_api` telt nu `n_geladen = len(regelingen) - n_skipped` en draait de
subdiv-herbouw alleen als `n_geladen > 0` (of bij `force`). Bij niets-nieuw zijn
de locaties ongewijzigd, dus is de subdiv-tabel al actueel. Een volledige
`--full-p2p` valt daarmee terug naar minuten; een incrementele sync naar
seconden.

---

## Zuinig bevragen van de DSO-API (voorkom key-blokkade)

- **Rate-limiter** (`src/rate_limiter.py`): 50 concurrent, 50 req/s. Alle
  DSO-calls lopen hier doorheen. Blijf hier ruim onder de fair-use-policy.
- **Nooit onbegrensd pagineren.** `find_regelingen` en `find_regelingen_delta`
  stoppen zodra er geen `_links.next` meer is (of, bij de delta, zodra het
  registratietijdstip ouder dan `sinds` is). Nooit "voor de zekerheid"
  doorvragen.
- **Skip-guard vóór laden.** Een al-geladen FRBR-expressie wordt lokaal (1 ms)
  herkend en niet opnieuw opgehaald — geen documentstructuur-, annotatie- of
  geometrie-calls.
- **Delta boven volledige sweep.** De `_sort=-registratietijdstip`-delta raakt
  alleen bronhouders met nieuwe registraties; een niks-nieuw-sync doet daarmee
  vrijwel geen API-calls.
- **Retries met mate.** Transiënte 503's (de DSO geeft er af en toe een) worden
  beperkt herprobeerd, niet in een strakke lus: `src/http_retry.py`, backoff
  2/5/15/30 s, alleen bij 5xx en timeouts — een 4xx gaat direct door. Elke retry
  loopt opnieuw door de rate-limiter.

  > **Dit gold tot 2026-08-01 alleen voor de KOOP-loader.** De
  > Presenteren-calls (`api_loader._get`, `dso_omgevingsvergunning._fetch_page`)
  > hadden géén retry, terwijl dit document het tegendeel beweerde. In de
  > sync-run van 2026-08-01 kostte dat twee fasen: de BOPA-snapshot brak af op
  > pagina 62 van 78 en één gemeente viel uit de i2a-fase — beide door één
  > losse 503. Nu gedekt, met tests in `tests/test_http_retry.py`.

---

## p2p incrementeel: de registratietijdstip-delta

De Presenteren v8 `/regelingen`-lijst geeft alle regelingen (~1966, 10 pagina's
van 200) met per regeling `geregistreerdMet.tijdstipRegistratie`.

> **Naamgeving:** sorteersleutel heet `registratietijdstip` (query-param
> `_sort`); het antwoordveld heet `geregistreerdMet.tijdstipRegistratie`.
> Niet verwarren.

Algoritme (`find_regelingen_delta` → `load_delta` in `api_loader.py`, via
`p2p.run_delta`; `fase_p2p`/`bepaal_sinds` in `full_sync.py`):

1. `sinds` = start vorige geslaagde sync − 2 dagen (overlap onschadelijk dankzij
   de skip-guard).
2. Pagineer de **volledige** lijst en houd de registraties `>= sinds` over.
   Per work wint de nieuwst geregistreerde expressie.
3. Filter op de geconfigureerde bronhouder-codes (scope gelijk aan de reguliere
   sweep), groepeer per bronhouder, laad alleen die subset.

### ⚠️ Waarom de hele lijst en niet "stoppen bij de eerste oudere"

Tot 2026-08-01 vroeg de sweep `_sort=-registratietijdstip` en **brak af** bij
het eerste item ouder dan `sinds` — "nieuwste eerst, dus de rest is ouder".
**Die aanname is fout.** De lijst is niet strikt gesorteerd; gemeten op
2026-08-01 gaf pagina 1:

```
1  2026-07-30  gm0779
2  2024-08-07  gm0984   ← hier brak de sweep af
3  2026-07-29  gm1963
4  2026-07-28  gm1900
5  2026-07-28  gm1681
```

Elke run pakte daardoor alleen de éérste registratie op. Resultaat: **16 gemiste
regelingen** over ruim een maand (4 omgevingsplannen, 5 programma's, 2
projectbesluiten, 2 omgevingsvisies, waterschapsverordening,
aanwijzingsbesluit N2000, voorbeschermingsregels) — terwijl elke sync netjes
"0 fouten" rapporteerde. Zie vault `gaps.md` G-98.

De volledige sweep kost ~10 calls — mínder dan de 381 van de per-bronhouder-
sweep — dus er is geen reden om op sortering te vertrouwen. `_sort` wordt
bewust niet meer meegestuurd: een instabiele sortering kan over paginagrenzen
items dubbelen of overslaan. Regressietests staan in
`tests/test_regelingen_delta.py`.

**Beperking**: de sweep detecteert nog steeds geen intrekkingen/verwijderingen
— die staan simpelweg niet meer in de lijst. `preview_sync.py` doet die
omgekeerde diff wél (en splitst *verdrongen* van *verdwenen*);
`diff_dso_bronhouder_coverage.py` blijft de zwaardere variant. Zie G-91.

---

## De fasen op een rij (met gemeten timings)

| # | Fase | Wat het doet | Duur (gemeten 2026-07-24, incrementeel) |
|---|---|---|---|
| 0 | preflight | DB / schijf / API-key | seconden |
| 1 | snapshot | actualiteit vorige sync → `audit.*_hist` | seconden |
| 2 | dedup | ALA/normwaarde-restgroepen (idempotent) | seconden |
| 3 | **p2p** | Ow-regelingen via Presenteren (delta) | **seconden** (was ~3u door subdiv-bug) |
| 4 | i2a | IMTR toepasbare regels, 342 gemeenten + landelijke catalogus | **~3 min** |
| 5 | vth | KOOP-kennisgevingen + enrich + geometrie + BOPA | **~15–20 min** (van oudsher de langere) |
| 6 | post | backfill, repair-pons, drieslag-MV's, health | ~enkele minuten |
| 7 | embed | nieuwe chunks embedden (Ollama, resumable) | seconden bij weinig nieuw |

**Referentie volledige sync (2026-07-17):** 5,0 uur totaal, 212 nieuwe
regelingen — waarvan het leeuwendeel de subdiv-storm was. Met de fix + delta
zit een reguliere incrementele sync ruim onder het uur, gedomineerd door vth.

---

## Wat bewust NIET in de sync zit

- **Wro / IMRO2006**: landelijke PDOK-herparse (16 GB `planobject`), eigen
  operatie (`load-wro-imro2006`, ~24 min voor 2141 plannen).
- **MER-register**: aparte harvester-repo; inladen via `load-mer` (~seconden).

---

## Boekhouding, monitor & afbreken

- Elke fase draait binnen `load_run(...)` → rij in `core.load_run`
  (`started_at`/`finished_at`/`status`/`n_verwerkt`/`n_fout`). De
  **fase-duur** is dus per run beschikbaar en wordt in het
  data-actualiteit-dashboard getoond (zie `/v1/load-status`).
- Begin: snapshot vorige actualiteit → `audit.*_hist`. Eind: run-snapshot →
  `audit.sync_run` (`totalen`/`metrics`) voor de per-run-Δ.
- **Bij afbreken**: een gekilde run laat z'n `load_run` op `running` staan.
  Sluit af met `status='gefaald'` (constraint: `running`/`ok`/`deels`/`gefaald`)
  en zet `audit.sync_run.klaar_op`, anders toont het dashboard een spook-sync.

---

## Openstaande efficiëntie-kansen

1. **subdiv als post-fase-batch**: i.p.v. per bronhouder tijdens p2p, één
   gebundelde refresh na afloop voor alléén de gewijzigde bronhouders.
2. **i2a-delta**: i2a is nu ~3 min, maar pollt nog per gemeente; bij groei
   dezelfde delta-behandeling als p2p mogelijk.

## Bekende zwakke plekken (nog niet opgelost)

- **i2a bevraagt de RTR/STTR met een hardgecodeerde `datum: "10-04-2026"`**
  (`imtr_loader.py`, drie plekken). Een sync in augustus haalt dus de
  toestand van 10 april op. `preview_sync.py --i2a` gebruikt dezelfde datum,
  zodat de preview toont wat de loader zóú zien — niet wat vandaag geldt.
- **Geen delta voor i2a en vth op prod** (G-94): daarom `--skip-i2a --skip-vth`
  bij een prod-directe run; vth loopt via `refresh-koop-to-prod.ps1`.
- **De vectorindex hangt niet aan de pipeline** (G-97): nieuwe regelingen zijn
  pas semantisch vindbaar na een aparte embed-run, en `run_overnight.py`
  herbouwt `chunk_annotatie`/`chunk_categorie` volledig.
