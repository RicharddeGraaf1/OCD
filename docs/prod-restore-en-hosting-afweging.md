# Productie-DB restore & hosting-afweging

> Datum: 2026-06-06 · Status: besluit nodig vóór uitvoering
> Context: ontdekt tijdens de `koop`→`vth` schema-rename dat productie een
> onvolledige restore is. Dit document bundelt de bevindingen + de
> hosting-/kostenafweging voor de volledige restore.

## TL;DR

- De `ocd-api/.env` wijst naar de **lokale dev-DB** (`localhost:5434/dso`, 56 GB,
  PostgreSQL 16), **niet** naar productie. De schema-rename en alle eerdere
  "live"-verificaties betroffen dus de dev-DB.
- **Productie** (Railway `postgis-17`) draait nog **code van 1 juni** (pushes
  triggeren geen deploy) en de DB is een **onvolledige restore**.
- De volledige restore **kan niet op het huidige 50 GB-volume**: de dev-DB is
  **56 GB**. Eerst volume vergroten (of dataset afslanken — levert weinig op).
- Doorlooptijd bij uitvoering: **3-6 uur**, vooral bepaald door upload-bandbreedte.
- Kosten bij 56 GB PostGIS: managed Postgres (Neon/Supabase) realistisch
  **$50-130/mnd**; Railway-volume vergroten **~$30-50/mnd**; eigen VPS (Hetzner)
  **~€15-30/mnd** maar self-managed.

---

## 1. Hoe we hier kwamen — de DB-mismatch

| | Host | Versie | Inhoud |
|---|---|---|---|
| `ocd-api/.env` `DATABASE_URL` | `localhost:5434/dso` | PostgreSQL 16.9 | **volledige** dataset, 56 GB; hier is `koop`→`vth` hernoemd |
| Railway productie | `postgis-17.railway.internal/railway` | PostgreSQL/PostGIS 17 | **onvolledige** restore, nog `koop` |

De `.env` bleek naar localhost te wijzen — niet naar Railway. Daardoor:
- De `ALTER SCHEMA koop RENAME TO vth` (2026-06-03) raakte de **dev-DB**, niet prod.
- De "deploy live geverifieerd"-claim was onjuist (verificatie liep tegen dev-DB
  / een Cloudflare-cache).

## 2. Huidige productie-staat (vastgesteld via Railway GraphQL + endpoint-probes)

- **Code = 1 juni.** Pushes van 3/4 juni (`5e3108e`, `c0a74be`, `4be0ddf`)
  triggerden **geen** Railway-deploy — GitHub-auto-deploy staat blijkbaar uit.
  Bewijs: `/v1/viewer/regelmix` → 404 (zit pas in `4be0ddf`).
- **DB = onvolledige restore.**

| Endpoint | Status | Betekenis |
|---|---|---|
| `/v1/vergunningen`, `/stats` | 200 | `koop.*` aanwezig |
| `/v1/ponsenkaart/gemeenten` | 200 | `v2a.ponsenkaart_gemeente_stats` aanwezig |
| `/v1/adres`, `/v1/zoek` | 500 | `p2p.activiteit_locatieaanduiding` + `p2p.tekst_element` **ontbreken** (`UndefinedTable`) |

Kortom: alleen de vergunningen-laag (`koop`) + ponsenkaart-stats staan in prod;
de omgevingsplan-/bestemmingsplan-dataset (`p2p`/`wro`/`i2a`) niet.

## 3. De blocker: grootte vs. volume

| | |
|---|---|
| Railway PostGIS-volume **limiet** | **50 000 MB (50 GB)** |
| Nu in gebruik | 7,4 GB |
| Volledige dev-DB | **56 GB** |

56 GB past niet in 50 GB, en restore vraagt extra werkruimte (WAL, index-builds).
**→ volume moet groter, of de dataset moet kleiner.**

### Grootte per schema (dev-DB)

| Schema | Grootte | Wat |
|---|---|---|
| `wro` | 18 GB | bestemmingsplannen (IMRO) |
| `p2p` | 17 GB | omgevingsplannen (STOP/IMOW) |
| `vth` | 5,0 GB | vergunningkennisgevingen |
| `i2a` | 0,7 GB | toepasbare regels (IMTR) |
| `core` | 10 MB | lookups |

### Grootste tabellen

| Tabel | Totaal | Data | Indexen | Opmerking |
|---|---|---|---|---|
| `wro.planobject` | 16 GB | 9,0 GB | 1,1 GB | bulk; bestemmingsplan-geometrie |
| `p2p.locatie_subdiv` | 8,0 GB | 7,5 GB | 0,35 GB | **afgeleid** — herbouwbaar via `refresh_locatie_subdiv()` |
| `vth.vergunningkennisgeving` | 5,1 GB | 1,3 GB | 0,57 GB | ~3 GB is TOAST (`raw_xml`/`inhoud_xml`-blobs) |
| `p2p.locatie` | 5,0 GB | 31 MB | — | geometrie in TOAST |
| `p2p.tekst_element` | 2,8 GB | 1,0 GB | 1,6 GB | |
| `p2p.naammatch_signaal` | 1,3 GB | 0,65 GB | 0,64 GB | **afgeleid** — materialized view |

### Afslanken levert weinig op

- **Blobs droppen** (`vth.raw_xml`/`inhoud_xml`, ~3 GB) of naar een ander schema
  verplaatsen: bespaart ~3 van 56 GB. Niet de moeite, en kost functionaliteit
  (volledige bekendmakings-tekst + her-parse-mogelijkheid).
- **Afgeleide tabellen excluden** (`locatie_subdiv` 8 GB, `naammatch_signaal`
  1,3 GB) en op prod herbouwen: scheelt ~9 GB **transfer**, maar na herbouw is de
  eind-footprint weer ~56 GB → **lost de volume-constraint niet op**.
- De bulk (`p2p` + `wro` = 35 GB) ís wat de viewers serveren — daar valt zonder
  functieverlies niet te snoeien.

**Conclusie:** het volume moet simpelweg groeien (naar ~80-100 GB voor 56 GB data
+ index-/werkruimte-overhead).

## 4. Versie-mismatch (PG16 → PG17)

Dev draait PostgreSQL 16.9, prod is PostgreSQL/PostGIS 17. Te overbruggen met de
**PG17-client** (`C:\Program Files\PostgreSQL\17\bin\pg_dump.exe`): een PG17-dump
van een PG16-server, gerestored op een PG17-server, is voorwaarts compatibel.
PostGIS-extensieversies moeten op prod ≥ die van dev zijn (PostGIS 17-image = ok).

## 5. Tijdsschatting (bij uitvoering)

| Fase | Schatting | Bepaald door |
|---|---|---|
| `pg_dump -Fc` (56 GB → ~10-20 GB gecomprimeerd) | 30-60 min | lokale schijf/CPU |
| **Upload naar Railway** | **20 min – 3 uur** | **upload-bandbreedte** (grootste onbekende) |
| `pg_restore` + indexen/PostGIS-GiST herbouwen | 1-3 uur | Railway-instance CPU/RAM |
| `refresh_locatie_subdiv()` + matviews (indien geëxcludeerd) | 15-45 min | |

**Totaal: ~3-6 uur**, leunend op upload-snelheid en de Railway-instance.

## 6. Kostenvergelijking — 56 GB PostGIS

> ⚠️ Prijzen bij benadering (kennis-cutoff jan 2026); **verifieer actueel**.
> Alle vier ondersteunen PostGIS. Kostendrijver bij dit formaat = opslag + compute.

| Optie | Indicatie/mnd | Voor- | Nadeel |
|---|---|---|---|
| **Railway** (volume → ~80-100 GB) | **~$30-50** | laagste frictie, al opgezet; één resize | opslag ~$0,25/GB/mnd + compute; boven budget |
| **Neon** (Scale + opslag) | **~$50-100** | serverless, autoscaling, branching; scale-to-zero | opslag-zwaar geprijsd; spatial-perf vraagt grotere compute |
| **Supabase** (Pro + disk + compute-add-on) | **~$75-135** | mooi DX, ingebouwde auth/PostGIS | 56 GB vraagt Medium/Large compute-add-on → snel duur |
| **VPS** (bijv. Hetzner CPX31/CX52) | **~€15-30** | **goedkoopst**, volledige controle, ruim disk | **self-managed**: backups, updates, security, uptime zelf doen |

**Observatie:** bij 56 GB breekt elke managed-Postgres het eerdere budget van
~$15-30/mnd (voor álle viewers samen). Alleen een **VPS** blijft binnen budget,
maar verschuift de operationele last naar ons.

### Grove richting

- Wil je **managed + minimale frictie** en is ~$30-50/mnd acceptabel:
  **Railway-volume vergroten** (we zitten er al).
- Wil je **laagste kosten** en is self-hosting acceptabel: **Hetzner-VPS** met
  zelf-beheerde Postgres+PostGIS (+ `pg_dump`-cron naar object-storage als backup).
- Neon/Supabase zijn bij dit formaat duurder dan Railway-resize en voegen voor
  deze use-case (één DB achter een eigen API) weinig toe.

## 7. Open beslissingen

1. **Waar hosten?** Railway-resize (laagste frictie) vs. VPS (laagste kosten) vs.
   managed elders.
2. **Upload-bandbreedte?** Bepaalt de transfer-tijd (20 min vs. 3 uur).
3. **Afgeleide tabellen excluden uit de dump** (en op prod herbouwen) om transfer
   te bekorten? Aanrader: ja voor `locatie_subdiv` (8 GB) en `naammatch_signaal`.

## 8. Correcte restore-procedure (zodra besloten)

> **De rename komt mee in de restore.** De dev-DB heeft al `vth` (daar hernoemd
> op 2026-06-03), dus de dump bevat `vth` — **geen aparte `ALTER SCHEMA` op prod**.
> Het oude, partiële `koop`-schema in prod zit níét in de dump en blijft bij een
> additieve restore staan als stale duplicaat → daarom prod eerst **schoon
> opruimen**. Klaargezet: cleanup-SQL `scripts/2026-06-prod-cleanup-before-restore.sql`
> + runbook `scripts/restore-dev-naar-prod.ps1`.

> ⚠️ **Downtime**: tijdens stap 4-6 is de vergunningen-viewer (die nu prod-`koop`
> queryt) tijdelijk uit de lucht tot de restore klaar is (~uren). Acceptabel voor
> een civic-tech-tool, maar plan het bewust.

**Prerequisites (go-moment, kost/risico):**
- **Volume vergroten** naar ~100 GB (56 GB data + index-/werkruimte) — kosten ↑.
- **Tijdelijke TCP-proxy** op de PostGIS-service (prod-DB is anders niet van
  buiten bereikbaar) — na afloop weer uitzetten.

**Stappen (geautomatiseerd in `restore-dev-naar-prod.ps1`):**
1. **Volume vergroten** op de Railway PostGIS-service naar ~100 GB.
2. **TCP-proxy aanzetten** → publieke `…proxy.rlwy.net:PORT`-connectstring ophalen.
3. **Dump dev-DB** (PG17-client):
   `pg_dump -Fc -Z6 --no-owner --no-acl --exclude-table-data=p2p.locatie_subdiv -d <dev-url> -f dso.dump`
   (`locatie_subdiv` = 8 GB afgeleid → uitsluiten, na restore herbouwen).
4. **Prod opschonen**: `2026-06-prod-cleanup-before-restore.sql` dropt de oude
   app-schema's (`koop`, partiële `p2p`/`v2a`/etc.) — `public`/PostGIS blijft.
5. **Restore**: `pg_restore --no-owner --no-acl --no-comments -j4 -d <prod-url> dso.dump`
   (extensie-`already exists`-meldingen zijn onschuldig).
6. **Afgeleide data herbouwen**: `SELECT refresh_locatie_subdiv();` +
   `REFRESH MATERIALIZED VIEW`-en (naammatch_signaal, ponsenkaart_gemeente_stats).
7. **TCP-proxy weer uitzetten.**
8. **Deploy nieuwe code** (`origin/main` t/m `4be0ddf`) — auto-deploy fixen of
   handmatige redeploy.
9. **Verifiëren**: `/v1/adres`, `/v1/zoek`, `/v1/vergunningen` → 200 tegen `vth.*`.

## Verwijzingen

- Schema-rename + correctie: zie `model.md` v1.7.5/v1.7.6 en `log.md` (vault),
  en het OCD-dashboard.
- Bestaande deploy-strategie: `dso-loader/DEPLOY.md`.
- Migratie-script rename: `dso-loader/scripts/2026-06-rename-koop-to-vth.sql`.
