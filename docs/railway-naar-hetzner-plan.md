# Railway naar Hetzner — alles wat er draait

> **Scope**: de verhuizing van al Richards projecten op Railway naar Hetzner Cloud met Coolify:
> wat er staat, wat er moet gebeuren voordat iets verhuist, de volgorde, het recept per app en
> het opruimen van Railway.
> **Niet in scope**: Locus zelf. Die verhuizing loopt al bij Thomas, zie
> [hosting-naar-hetzner.md](c:/GIT/Locus/docs/plans/hosting-naar-hetzner.md) en
> [infrastructure.md](c:/GIT/Locus/docs/architectuur/infrastructure.md).
> **Status**: in uitvoering, 2026-10-27. Besluiten B1–B4 genomen, V1, V2 en V5 (op V5.2 na)
> klaar. woordjacht-API en omgevingsbot live op Coolify sinds 2026-10-27, **ocd** (database,
> ollama-embed, ocd-api op `api.ponsenkaart.nl`) sinds 2026-10-28. Football-Faceoff: data over,
> app bouwt niet. Nog te doen: RichFit, Bundle of Recipes, Railway opruimen. Zie [Verloop](#verloop).
> **Vorige afweging**: [prod-restore-en-hosting-afweging.md](prod-restore-en-hosting-afweging.md) (2026-06-06, toen gekozen voor Railway-resize).

## Waarom

Thomas zet de server `apps-1` (Hetzner, Neurenberg, Coolify) neer voor alle applicaties, ook die
van Richard (besluit Thomas, 2026-10-22). Richard wil alles van Railway af, niet alleen `ocd`
(2026-10-27). Een grotere schijf op `apps-1` is voor Richard geen bezwaar.

## Wat er nu op Railway draait

Gemeten op 2026-10-27 met de Railway-CLI, werkruimte `richarddegraaf1's Projects`, regio
`europe-west4` (Amsterdam). Locus staat er ook, maar valt buiten dit plan.

| Project | Service | Bron en build | Domein | Data |
|---|---|---|---|---|
| `ocd` | `ocd-api` | repo `RicharddeGraaf1/OCD`, map `/ocd-api`, Dockerfile, health `/health` | `api.ponsenkaart.nl` (Cloudflare) | — |
| | `PostGIS 17` | image `postgis/postgis:17-master` | — (TCP-proxy aan) | volume 94 GB van 150 GB; database 84 GB |
| | `ollama-embed` | **geen repo**: met `railway up` geplaatst | intern `ollama-embed.railway.internal:11434` | model nomic-embed-text |
| `omgevingsbot.nl` | `backend` | repo `omgevingsbot.nl`, map `/backend`, **Nixpacks** | alleen `backend-production-38d9.up.railway.app` | — |
| | `frontend` | zelfde repo, map `/frontend`, Dockerfile | `www.omgevingsbot.nl` | — |
| | `Postgres` | `postgres-ssl:18` | — (publieke URL aan) | volume 1,1 GB |
| `woordjacht` | `Conteo_website` | repo `Conteo_website`, map `/api`, **Railpack** | `api.elconteo.nl` | — |
| | `Postgres` | `postgres-ssl:18` | — | volume 0,8 GB, ranglijst |
| `richfit` | `app` | **geen repo-koppeling** (lokaal `c:/GIT/RichFitApp`), Dockerfile | `rich-fit.nl`, `www.rich-fit.nl` (TransIP) | volume `/app/data` 0,9 GB |
| | `Postgres` | `postgres-ssl:18` | — | volume 0,9 GB, **trainingshistorie van gebruikers** |
| `bundle-of-recipes` | `frontend`, `scraper` | **geen repo-koppeling**, Railpack | `bundleofrecipes.nl` | — |
| | `backend` | **geen repo-koppeling**, Dockerfile (Spring) | — | — |
| | `Postgres` | `postgres-ssl:18` | — | volume 0,9 GB, **accounts en recepten** |
| `Football-Faceoff` | `FootballFaceoff` | repo `FootballFaceoff`, Dockerfile | Railway-domein | **gestopt sinds 2026-05-13** |
| | `Postgres`, `Redis` | templates | — (publieke URL aan) | 1,1 GB + 1,1 GB, draaien nog door |

De volumes van de kleine Postgres-databases zijn ~1 GB, maar dat is vooral de basisinstallatie en
WAL. De echte data is kleiner.

### Wie er van buitenaf bij de data komt

Dit is het lastigste stuk van de verhuizing.

| Wie | Waar naartoe | Hoe nu |
|---|---|---|
| Richards laptop: `full_sync.py --target prod`, `repliceer_*_naar_prod.py`, backfills, DDL op prod | OCD-database | TCP-proxy van Railway, `PROD_DB_URL` in `dso-loader/.env` |
| GitHub Action van `instructieregels.nl` (maandag 06:00 UTC) | OCD-database | `PSQL_CONN` als secret, via de TCP-proxy. Er is geen read-only gebruiker: op prod bestaat alleen de rol `postgres` |
| Cloudflare Pages-functie van ponsenkaart.nl | `ocd-api` | `UPSTREAM = 'https://ocd-api-production.up.railway.app'` in [`functions/api/[[catchall]].js`](c:/GIT/ponsenkaart.nl/functions/api/[[catchall]].js), dus niet via `api.ponsenkaart.nl` |
| Omgevingsbot-backend | `ocd-api` | `OCD_API_URL = https://ocd-api-production.up.railway.app` |
| Omgevingsbot-frontend en externe ontwikkelaars (`DEV_API_KEYS`) | omgevingsbot-backend | `BACKEND_URL = https://backend-production-38d9.up.railway.app`, er is geen eigen domein |

Een Railway-adres (`*.up.railway.app`) verdwijnt met de service. Alles in deze tabel moet dus om
vóórdat Railway weg kan. **Correctie 2026-10-28**: hier stond dat OCD-viewer,
omgevingsdocumentenregister en omgevingsvergunning-register al `api.ponsenkaart.nl` gebruikten.
Dat klopte niet: hun Pages-proxy wees ook naar `ocd-api-production.up.railway.app`. Omgezet op
2026-10-28, zie [Verloop](#verloop).

### DNS

Opgezocht op 2026-10-27.

| Domein | Zone | Nu |
|---|---|---|
| `api.ponsenkaart.nl` | Cloudflare | naar Railway, **proxy uit** (antwoordt met `Server: railway-hikari`, zonder `cf-ray`; [PONSENKAART-DEPLOY.md](../PONSENKAART-DEPLOY.md) zegt "proxy aan") |
| `www.omgevingsbot.nl` | Cloudflare | proxy aan |
| `bundleofrecipes.nl` | Cloudflare | naar Railway, proxy uit |
| `api.elconteo.nl` | Cloudflare | naar Railway, proxy uit |
| `rich-fit.nl`, `www.rich-fit.nl` | TransIP | ALIAS en CNAME naar Railway |

## Besluiten

Genomen door Richard op 2026-10-27.

### B1. Alles naar `apps-1`, ook de privé-apps

RichFit, Bundle of Recipes en Football-Faceoff gaan voorlopig ook naar `apps-1`. Wat daarbij
blijft gelden:

- **Backups regelen we zelf.** Op `apps-1` staan backups uit, omdat het voor Locus een
  speelomgeving is ("dataverlies is in deze fase geen probleem", Thomas 2026-10-22). Voor RichFit
  (trainingshistorie) en Bundle of Recipes (accounts) geldt dat niet. Daarom eerst V3, dan pas die
  twee.
- **Bundle of Recipes** is de repo van Iris (`irisjona/bundle-of-recipes`): zij moet de GitHub App
  toelaten, of de repo verhuist (V2).
- **Football-Faceoff** verhuist zoals hij nu is: database en Redis mee, de app gebouwd maar
  **gestopt**, zoals op Railway sinds 2026-05-13.
- **Als Locus naar productie gaat**, gaat `apps-1` mogelijk uit elkaar (Thomas). Dan verhuizen de
  privé-apps nog een keer; met de backups uit V3 is dat een restore.

### B2. SSH-tunnel naar de OCD-database

De database krijgt geen publieke poort. PostGIS publiceert alleen op `127.0.0.1:5433` van
`apps-1`, en de Cloud Firewall laat 5433 niet door.

- **Laptop**: `ssh -N -L 5433:127.0.0.1:5433 root@2.31.94.124` (eigen sleutel, V1), en dan
  `PROD_DB_URL=postgresql://postgres:<wachtwoord>@localhost:5433/railway` in `dso-loader/.env`.
  Kies lokaal een poort die niet botst met de lokale database (5434).
- **GitHub Action van `instructieregels.nl`**: een eigen gebruiker op de server die alleen mag
  tunnelen (`restrict,port-forwarding,permitopen="127.0.0.1:5433"` in zijn `authorized_keys`), met
  zijn sleutel als secret, en een **read-only rol** in de database
  ([db-readonly-user.sql](../ocd-api/db-readonly-user.sql), op prod nooit gedraaid).
  GitHub Actions komen vanaf wisselende adressen, dus SSH in de firewall `beheer` moet open voor
  iedereen; dat kan, want root logt alleen in met een sleutel. Dit is een afspraak met Thomas. Het
  alternatief: de build als Scheduled Task in Coolify, dan blijft SSH dicht.
- **Prod blijft herkenbaar als prod**: de scripts herkenden prod aan `rlwy.net` in de
  verbindingsstring. Via de tunnel is dat `localhost`, en dan liet de blokkade in de Wro-scripts
  ("alleen lokaal") prod gewoon door. Opgelost in V5.4.

### B3. Schijf: pas groter als het nodig is

> **Bijgewerkt 2026-10-28: de schijf is groter gemaakt.** `apps-1` is nu een volle CPX42 met
> 320 GB (301 GB bruikbaar), zonder meerprijs: jullie betaalden al de CPX42-prijs, alleen de
> schijf was blijven staan. Onomkeerbaar (terug naar CPX32 kan niet meer). De rest van B3 hieronder is
> daarmee achterhaald, maar blijft staan als onderbouwing.

Richard en Thomas vinden een grotere schijf allebei prima, maar pas als het echt nodig is. Dus
zonder opschalen beginnen, en zo verhuizen dat er geen tweede kopie op de schijf staat:

| Op de schijf van 150 GB | GB |
|---|---|
| Nu in gebruik (2026-10-27, 18:20), waarvan 16 GB build-cache van Locus | 29 |
| `ocd`-database (vers teruggezet, zonder de 10 GB extra van het Railway-volume) | 84 |
| Kleine databases, images, Ollama-model | ~8 |
| **Samen** | **~121 (81%)** |

De build-cache van Locus houdt Thomas bewust warm, zodat de eerste build van de dag niet koud
is (hosting-naar-hetzner.md, stap 2). Daardoor komt `ocd` direct boven de 80%. **Vóór `ocd`
met Thomas kiezen**: de build-cache begrenzen (bijvoorbeeld 8 GB via `docker builder prune
--keep-storage`), of de schijf meteen groter.

- **Overzetten zonder dumpbestand**: per schema een dump in mapformaat (`-Fd -j 4`), meteen
  terugzetten met `pg_restore -j 6`, en de map weggooien voor het volgende schema. De grootste is
  `p2p` (35 GB in de database; de dump is kleiner, maar geometrie en vectoren comprimeren slecht).
  Zo is er nooit meer dan één schema extra op de schijf, en is het toch parallel. Het kan ook in
  één stroom (`pg_dump -Fc | pg_restore`), zonder extra ruimte, maar dan zonder parallelisme en
  dus trager.
- **Wanneer opschalen**: als de schijf boven 80% (120 GB) komt. Dan de volle schijf van de CPX42;
  dat is onomkeerbaar en de server moet even uit, dus ook Locus. Afspraak met Thomas.
- Een Hetzner Volume valt af: het is netwerkopslag, trager dan de lokale NVMe, en de OCD-database
  heeft al last van een koude cache.

### B4. Geheugen

De Locus-build piekt op 10,1 GB van de 16 GB. Daarnaast komen PostGIS (nu `shared_buffers`
512 MB, liever 2–4 GB), `ocd-api`, Ollama (~1 GB) en de kleinere apps. Elke container krijgt een
geheugenlimiet in Coolify; we meten een week. Past het niet, dan is de volgende stap 32 GB
(CPX62, €130,99/mnd). Ook dat is een gesprek met Thomas, ook over hoe de kosten verdeeld worden.

## Voorbereiding

Na deze stappen draait er nog niets anders, maar staat alles klaar.

- [ ] **V1. Toegang.** Je SSH-sleutel op `apps-1`; Thomas zet hem erop (zie "Een beheerder
  toevoegen" in infrastructure.md). 2FA aan in je Coolify-profiel.
- [ ] **V2. GitHub-bron voor je eigen repo's.** De GitHub App `coolify-locuslegis` geldt alleen
  voor de organisatie `LocusLegis`. Maak in Coolify een tweede GitHub App op je eigen account, met
  alleen leesrechten, voor `OCD`, `omgevingsbot.nl`, `Conteo_website`, `RichFitApp` en
  `FootballFaceoff`. De route `/webhooks/source/github/events` heeft in `admins-only.yaml` al een
  uitzondering op het filter, dus webhooks werken ook voor jouw app. Voor Bundle of Recipes moet
  Iris de app op haar repo toelaten, of de repo verhuist naar jouw account of een organisatie.
- [ ] **V3. Backups** voor jouw databases naar **Cloudflare R2** (besluit Richard, 2026-10-27):
  een ander bedrijf dan de server, en de kleine databases passen waarschijnlijk in de gratis
  10 GB. In het Cloudflare-dashboard een bucket (`coolify-backups`) en een R2-sleutel voor de S3-API
  (alleen die bucket, lezen en schrijven); de sleutel die nu in `.env` van ponsenkaart staat,
  mag alleen Pages en kan niet bij R2. In Coolify een S3-bestemming met die sleutel, en per
  database een dagelijkse `pg_dump`. Eén keer een restore testen. Verplicht vóór RichFit en Bundle of Recipes (B1).
  **`ocd` krijgt geen backup** (besluit Richard, 2026-10-30): prod en de lokale database vangen
  elkaar op. Prod is volledig afgeleid van lokaal (elke sync, en de restore van 28-10 bewees de
  herbouw); de oordelen en hertalingen die lokaal met Sonnet zijn gemaakt, staan via de sync ook op
  prod. Wat bij dit besluit op één plek blijft: de gouden set en de vth-tabellen van
  `feat/vergunning-regelkoppeling` (alleen lokaal), en de MER-store `harvest/data/mer.db` (laptop).
  Het R2-deel van V3 geldt dus alleen nog voor RichFit en Bundle of Recipes, en kost geld zodra
  R2 een betaalmethode vraagt; voor die twee nog te kiezen.
- [ ] **V4. TTL omlaag.** Een dag van tevoren de TTL van elk domein dat verhuist naar 300 s. Op
  Cloudflare: `api.ponsenkaart.nl`, `www.omgevingsbot.nl`, `bundleofrecipes.nl`,
  `api.elconteo.nl`. Op TransIP: `rich-fit.nl` en `www`. Zie [DNS](#dns).
- **V5. Codewijzigingen.** Zo gemaakt dat ze nu op Railway al werken.
  - [x] **V5.1 ponsenkaart-proxy** (2026-10-27, lokaal, niet gecommit): `UPSTREAM` in
    [`functions/api/[[catchall]].js`](c:/GIT/ponsenkaart.nl/functions/api/[[catchall]].js) is nu
    `https://api.ponsenkaart.nl`. Dat adres antwoordt nu al (200 op `/health`). Na de deploy van
    ponsenkaart.nl verhuist de proxy vanzelf mee met de DNS.
  - [x] **V5.2 Omgevingsbot** (2026-10-27, op Coolify ingericht): de backend is de chatlaag (vraag ontvangen, regels ophalen bij
    `ocd-api` of live bij het DSO, antwoord laten schrijven door Groq, sleutels en feedback). De
    website praat er niet rechtstreeks mee: `frontend/server.js` stuurt `/api/*` door naar
    `BACKEND_URL`. Op Coolify kan dat dus over het interne netwerk, zonder publiek adres. Alleen
    als externe ontwikkelaars (`DEV_API_KEYS`) de backend nog rechtstreeks gebruiken, heeft hij
    een eigen domein nodig (voorstel `api.omgevingsbot.nl`). `OCD_API_URL` gaat naar
    `https://api.ponsenkaart.nl`, of intern naar `ocd-api`.
  - [x] **V5.3 curl in `ocd-api`** (2026-10-27, lokaal): de health-check van Coolify draait curl
    of wget in de container, en `python:3.12-slim` heeft geen van beide.
    [Dockerfile](../ocd-api/Dockerfile).
    - [ ] Hetzelfde voor de Bundle-backend (`eclipse-temurin:21-jre`), via Iris. De alpine-images
      (omgevingsbot-frontend, RichFit, Football-Faceoff) hebben `wget` via busybox.
  - [x] **V5.4 Prod herkennen ook via de tunnel** (2026-10-27, lokaal): `is_prod_url()` in
    [src/db.py](../dso-loader/src/db.py) ziet prod aan de DSN uit `PROD_DB_URL`, niet alleen aan
    `rlwy.net`. Gebruikt in `get_conn()` (parallelisme uit), `full_sync.py` en
    `prune_verouderde_versies.py` (label en typbevestiging), en in de "alleen lokaal"-blokkade van
    `bouw_wro_indeling.py`, `laad_wro_teksten_v2.py` en `laad_wro_regels_html.py`. Zonder dit
    schreven die drie via de tunnel gewoon in prod. 100 tests groen.
  - [x] **V5.5 PostGIS-image** (2026-10-27, lokaal): [ocd-postgis/Dockerfile](../ocd-postgis/Dockerfile),
    `postgis/postgis:17-3.6` (PostGIS 3.6.4) met pgvector 0.8.7. Prod draaide `17-master`, een
    meeschuivende tag met PostGIS 3.7.0dev en pgvector 0.8.4. **Restore-proef gedaan**: de
    volledige structuur van prod (`pg_dump --schema-only`) komt foutloos over. 147 tabellen,
    15 materialized views, 12 views, 385 indexen en 152 functies, gelijk aan prod. De enige twee
    meldingen: schema's `tiger` en `topology` bestonden al, omdat het image die zelf aanmaakt.
    De data zelf is nog niet geproefd.
  - [x] **V5.6 `ollama-embed`** (2026-10-27, lokaal): [ollama-embed/Dockerfile](../ollama-embed/Dockerfile),
    `ollama/ollama:0.31.2`, dezelfde versie als op Railway. Haalt `nomic-embed-text` op bij de
    start. In Coolify een volume op `/root/.ollama`. **Lokaal getest**: het image start, haalt
    `nomic-embed-text` op met hetzelfde image-id als op Railway (`0a109f422b47`) en geeft
    embeddings van 768 dimensies.

Committen: in OCD gaan V5.3–V5.6 en dit plan als eigen commit naar `main`. Een push naar `main`
deployt `ocd-api` op Railway; curl erbij is daar onschadelijk. ponsenkaart.nl (V5.1) deployt via
`deploy.sh`.

## Volgorde

Van klein naar groot. Wat geen gebruikersdata heeft, eerst. De eerste verhuizing is ook de
oefening met Coolify.

| # | Wat | Waarom hier | Onderbreking |
|---|---|---|---|
| 1 | woordjacht-API (`api.elconteo.nl`) | één service: de oefenronde. **Live op Coolify** | geen: de lobby zit in het geheugen |
| 2 | `ocd`: PostGIS, `ollama-embed`, `ocd-api` | het grootste stuk. **Live op Coolify** (2026-10-28) | geen |
| 3 | omgevingsbot (backend, frontend, Postgres) | kan vóór 2: praat via `api.ponsenkaart.nl`. **Live op Coolify** | geen |
| 4 | Football-Faceoff | database en Redis zonder actieve app. **Data over; app bouwt niet (codefout)** | geen |
| 5 | RichFit | na V3 (backups) | minuten, buiten de trainingstijden |
| 6 | Bundle of Recipes | na V3 en akkoord van Iris | minuten |
| 7 | Railway opruimen | na een week zonder verkeer per project | — |

## Recept per app

1. **Database** in Coolify: hetzelfde hoofdversienummer (Postgres 18), vastgezet op een tag,
   alleen intern bereikbaar, backup-schema aan (V3).
2. **App** uit GitHub met dezelfde buildwijze. Coolify kent Dockerfile en Nixpacks (de
   omgevingsbot-backend). Railpack (woordjacht-API, Bundle-frontend en -scraper) zit in Coolify
   als **bèta** en alleen voor Git-apps
   ([docs](https://coolify.io/docs/applications/build-packs/railpack)): eerst proberen op het
   tijdelijke adres; lukt het niet, dan Nixpacks of een korte Dockerfile. Bundle-frontend en
   -scraper zijn op Railway met `railway up` geplaatst en hebben dus nog een Git-bron nodig (V2).
3. **Variabelen** van Railway overnemen. Waarden zet je zelf in Coolify, nooit in git.
   `DATABASE_URL` wijst naar de containernaam van de nieuwe database. Variabelen alleen bij het
   draaien, niet tijdens de build.
4. **Tijdelijk adres**: het sslip.io-domein dat Coolify geeft, of een subdomein. Eerst testen met
   een lege of gekopieerde database.
5. **Data overzetten**: schrijven stopzetten (app op Railway in onderhoud of gestopt), dan
   `pg_dump -Fc` via de publieke URL of TCP-proxy van Railway, `pg_restore` in de nieuwe
   container, en rijen tellen per tabel aan beide kanten. Redis van Football-Faceoff start leeg:
   de app bewaart er alleen lobbystatus in, met een vervaltijd (`LobbyContext.ts`, `set ... EX`).
6. **DNS omzetten**, certificaat van Let's Encrypt afwachten, rooktest.
7. **Railway laten staan** zonder verkeer, een week. Daarna het project verwijderen.

### Bijzonderheden `ocd`

- **Overzetten met [overzetten.sh](../ocd-postgis/overzetten.sh)**, op `apps-1` zelf, niet via de
  laptop. Het script doet: extensies en `search_path` zoals op de bron; de structuur zonder
  indexen; per schema de data (map in mapformaat, terugzetten, map weg, dus nooit een tweede
  kopie, zie B3); daarna alle indexen en sleutels parallel; ANALYZE; en per tabel het aantal rijen
  vergelijken met Railway. Elke onverwachte fout stopt alles.
  **Geproefd op 2026-10-27** van Railway naar een lokale container, met schema `skos`: alle
  stappen groen, rijen gelijk. Die proef vond ook een fout die bij de echte overzetting was
  misgegaan: `pg_trgm` en `vector` staan op prod in schema `p2p`, niet in `public`, en indexen
  verwijzen naar `p2p.gin_trgm_ops`. Het script maakt elke extensie nu in hetzelfde schema als
  op de bron. Railway in Amsterdam naar Neurenberg is datacenter naar datacenter: veel sneller
  dan de thuisverbinding. Reken op een paar uur, vooral voor het opnieuw opbouwen van de GiST-
  en vectorindexen.
- **Geen onderbreking.** Gebruikers lezen alleen. Doe geen sync tussen de dump en de
  DNS-overstap. Dan is de kopie gelijk, en mag Railway blijven antwoorden tot de DNS om is.
- **`/dev/shm`.** Docker geeft een container standaard 64 MB gedeeld geheugen; parallelle
  queries en `REFRESH MATERIALIZED VIEW` vielen daar op Railway al op om. Geef de
  PostGIS-container in Coolify `--shm-size=2g` (Custom Docker Options). Dan kan het parallelisme
  op prod weer aan. `get_conn()` zet het nu nog uit voor elke prod-verbinding; dat kan na de
  overstap weg.
- **Instellingen** meenemen of verbeteren: `shared_buffers` (nu 512 MB), `work_mem`,
  `maintenance_work_mem` hoger tijdens de restore. Plus het `search_path` op database-niveau: dat
  ging bij de restore van juni mis.
- **De naam `railway` voor de database** kan blijven, dan hoeft er in de scripts niets om.
- **Cloudflare ervoor?** `api.ponsenkaart.nl` staat nu met de proxy uit. Op Hetzner wordt het een
  A-record naar `2.31.94.124`. De proxy aanzetten (SSL op Full (strict)) geeft DDoS-bescherming en
  verbergt het IP-adres van de server. Let's Encrypt via HTTP-01 werkt daar doorheen, zolang
  poort 80 open is (firewall `web`). Advies: proxy aan.
- **Sync daarna**: `PROD_DB_URL` naar de tunnel (B2); de GitHub Action van `instructieregels.nl`
  naar de tunnel, met de read-only rol.

## Opruimen

- Railway-projecten verwijderen, na een week zonder verkeer per project.
- Railway-sporen uit de repo's: `ocd-api/railway.toml`, `omgevingsbot.nl/*/railway.toml`,
  `FootballFaceoff/railway.json` en de workflow `deploy.yml` met `RAILWAY_TOKEN`, de secrets
  `RAILWAY_TOKEN` en `PSQL_CONN` op GitHub, en de `rlwy.net`-herkenning in `is_prod_url()`.
- Documentatie: [ARCHITECTUUR.md](../ARCHITECTUUR.md) (productie-database, TCP-proxy),
  [PONSENKAART-DEPLOY.md](../PONSENKAART-DEPLOY.md), [synchronisatie-runbook.md](synchronisatie-runbook.md),
  [prod-deploy-recovery-runbook.md](prod-deploy-recovery-runbook.md),
  `RichFitApp/docs/deploy.md`, en de dashboardbestanden in `c:/GIT/ProjectenDashboard/projecten/`.
- Thomas vragen de tabel in [infrastructure.md](c:/GIT/Locus/docs/architectuur/infrastructure.md)
  bij te werken (die repo is van hem).
- Railway-abonnement opzeggen als ook Locus weg is (dat project staat in jouw werkruimte).

## Verificatie

- [x] B1–B4 besloten (Richard, 2026-10-27)
- [x] De structuur van prod komt foutloos over in het nieuwe PostGIS-image (2026-10-27)
- [ ] Met Thomas afgesproken: SSH open voor de tunnelgebruiker (B2), geheugen meten (B4)
- [ ] Een restore uit de backup van één database lukt (V3)
- [ ] Per app: health-check groen in Coolify, rooktest op het eigen domein, certificaat geldig
- [ ] `ocd`: rijen per schema gelijk aan Railway; `/v1/adres`, `/v1/zoek`, `/v1/vergunningen` en
      een plandetail van een oud bestemmingsplan geven 200; een semantische zoekvraag geeft
      dezelfde top-5 als op Railway (zelfde embeddingmodel)
- [ ] `full_sync.py --target prod` draait via de tunnel, met het label PRODUCTIE in het rapport;
      `laad_wro_teksten_v2.py` met de tunnel-DSN in `DSO_DB` stopt met "alleen lokaal"
- [ ] De Action van `instructieregels.nl` draait groen met de read-only rol
- [ ] ponsenkaart.nl, OCD-viewer, mer-register, omgevingsbot en elconteo werken zonder één
      `*.up.railway.app` in hun verkeer (netwerktab)
- [ ] Schijf na de `ocd`-restore onder 80%
- [ ] Een week na de laatste overstap: Railway leeg, abonnement opgezegd

## Open vragen

1. Hoe verdelen Richard en Thomas de kosten van `apps-1`? Railway kost nu $65 per maand
   (Richard, 2026-10-27), inclusief Locus. `apps-1` kost €69,49; de 32 GB-stap (B4) €130,99.

## Verloop

### 2026-10-27: toegang, GitHub en de oefenronde

- **Toegang.** SSH-sleutels voor Richard (`richard@locuslegis-hetzner`) en voor zijn Claude
  (`claude-richard@locuslegis-hetzner`) op `apps-1`. Daarbij wiste een opruimregel per ongeluk
  ook `claude-thomas@locuslegis-hetzner`; die is na twee minuten teruggezet uit
  `authorized_keys.voor-hernoemen` (zelfde vingerafdruk `fcZoC…`). Richard licht Thomas in.
- **API-token** `claude-richard` voor Coolify: `read`, `write`, `deploy`, niet `root`, Root Team,
  verloopt 2026-11-26. Het token staat alleen op Richards laptop
  (`~/.coolify-token-claude-richard`).
- **GitHub App** `coolify-rdg-apps1` op het account RicharddeGraaf1, alleen leesrechten, voor
  OCD, omgevingsbot.nl, Conteo_website, RichFitApp en FootballFaceoff (V2).
- **woordjacht-API.** Project `woordjacht` in Coolify, app `woordjacht-api` uit
  `Conteo_website/api`, Railpack, poort 3000, geheugenlimiet 256 MB, health-check op `/gezond`
  (de Railpack-image heeft curl). `WOORDJACHT_SECRET` overgenomen van Railway.
  - De database van het Railway-project is **leeg en ongebruikt**: de API heeft geen
    Postgres-bibliotheek en houdt de lobby in het geheugen. Op Coolify dus geen database.
  - Eerste build en deploy in 49 s. Een push naar `main` deployt via de webhook (getest met
    `dbc641d`). `/gezond`, `/samen/nu` en CORS voor `https://elconteo.nl` gelijk aan Railway.
  - Domeinen: `https://api.elconteo.nl` en het tijdelijke
    `https://7oxqiqvbozxnjbhvmgh8i8ga.2.31.94.124.sslip.io`.
  - **Volgende stap**: in Cloudflare (`elconteo.nl`) het record `api` van CNAME
    `p37n5982.up.railway.app` naar A `2.31.94.124`, proxy uit. Daarna de app herstarten voor
    het certificaat, en een week later het Railway-project `woordjacht` opheffen.
- **omgevingsbot.** Project `omgevingsbot.nl` in Coolify:
  - `omgevingsbot-postgres`: `postgres:18-alpine`, alleen intern, 256 MB. De 11
    feedback-berichten van Railway gekopieerd (bron 11, doel 11).
  - `omgevingsbot-backend`: Nixpacks uit `/backend`, met het spaCy-model in de build (~4 min),
    poort 8000, 1 GB (gebruikt ~420 MB), health-check op `/api/health`. **Geen publiek adres**:
    vaste interne naam `omgevingsbot-backend` (consistent container name). Nadeel: bij een
    deploy van de backend een paar seconden geen antwoord. `OCD_API_URL` is
    `https://api.ponsenkaart.nl`; de andere variabelen overgenomen van Railway.
  - `omgevingsbot-frontend`: Dockerfile uit `/frontend`, poort 3000, 256 MB, health-check op `/`.
    `BACKEND_URL=http://omgevingsbot-backend:8000`.
  - Getest: dezelfde vraag (Broekhem 33, dakkapel) via `/api/ocd/chat` geeft op Coolify en op
    Railway **hetzelfde antwoord**, met dezelfde 10 bronnen, in ~3,3 s.
  - Domeinen: `https://www.omgevingsbot.nl` en het tijdelijke
    `https://qnqia3nzuagqrnfogdqfdc3k.2.31.94.124.sslip.io`. `omgevingsbot.nl` zonder www
    stuurt Cloudflare zelf door naar www; dat blijft zo.
  - **Volgende stap**: feedback nog één keer kopiëren, dan in Cloudflare (`omgevingsbot.nl`) het
    record `www` naar A `2.31.94.124`, eerst met de proxy **uit** (Let's Encrypt), na het
    certificaat weer aan, SSL op Full (strict). Een week later Railway-project
    `omgevingsbot.nl` opheffen.
  - **Image van 11,3 naar 3,7 GB.** `sentence-transformers` (de reranker) trok de CUDA-build van
    PyTorch binnen, met 3,2 GB aan NVIDIA-bibliotheken. Nu installeert
    `omgevingsbot.nl/backend/nixpacks.toml` eerst de CPU-versie (`d3982d9`, eerst op een aparte
    branch alleen op Coolify getest). Geldt ook voor Railway. De build duurt 4 in plaats van 9 min.
  - **Bevinding: de reranker kan in deze build niet laden**, ook niet op Railway en ook niet in
    het oude image: `import torch` faalt op een ontbrekende `libstdc++.so.6` in de
    Nix-omgeving. Hij staat nergens aan (geen `USE_SEMANTIC`- of `VECTOR_`-variabelen), dus niemand
    merkt het. Wie hem aanzet, heeft nodig: `libstdc++` in de build (in `nixpacks.toml`, bv.
    `nixLibs`), en meer dan 1 GB geheugen voor `BAAI/bge-reranker-v2-m3`.
- **Schijf na deze ronde**: 47 GB in gebruik, waarvan ~29 GB build-cache (Locus en de nieuwe
  builds). Coolify ruimt die 's nachts op (`docker_cleanup_frequency` 00:00, drempel 80%).

### 2026-10-27, avond: DNS om voor woordjacht en omgevingsbot

- Feedback van de omgevingsbot vlak ervoor opnieuw gekopieerd (11 = 11); daarna kwam er op
  Railway niets meer bij.
- Cloudflare (door Richards Claude in Chrome): `api.elconteo.nl` van CNAME
  `p37n5982.up.railway.app` naar A `2.31.94.124`; `www.omgevingsbot.nl` van CNAME
  `frontend-production-xxxx.up.railway.app` (proxied) naar A `2.31.94.124`. Beide **DNS only**,
  TTL Auto. Niets anders aangeraakt. SSL/TLS van `omgevingsbot.nl` staat op Full.
- Na een redeploy hebben beide een certificaat van Let's Encrypt (tot 2027-01-25). `/gezond`,
  CORS voor `https://elconteo.nl`, de site, `/api/models` en een chatvraag werken op de nieuwe
  server. `omgevingsbot.nl` (CNAME naar www, proxied) geeft via Cloudflare nog steeds de 301
  naar `www`.
- **Raadsel**: het oude `www`-record wees naar een niet-bestaande host (`xxxx`) en werkte toch.
  Er zit dus een Cloudflare-regel (Worker-route, Origin Rule of iets dergelijks) die verkeer
  voor `www.omgevingsbot.nl` naar Railway stuurde. Met de proxy uit doet die niets. **Niet
  de proxy aanzetten** voordat die regel gevonden en weg is, anders gaat `www` terug naar
  Railway (en na het opheffen naar niets). Advies: proxy voorlopig uit laten.
- **Traag of 500 bij sommige vragen**: niet door de verhuizing. `ocd-api` op Railway loopt bij
  `/v1/adres` tegen de statement timeout (koude cache: de PostGIS op Railway heeft 2 GB
  geheugen). De omgevingsbot op Railway had hetzelfde. Gaat weg als `ocd` verhuist.
- **Over een week (2026-11-03)**: Railway-projecten `woordjacht` en `omgevingsbot.nl` opheffen,
  na een laatste controle op feedback.

### 2026-10-27, avond: Football-Faceoff

- Project `Football-Faceoff` in Coolify: `footballfaceoff-postgres` (`postgres:18-alpine`, 256 MB)
  en `footballfaceoff-redis` (`redis:8.2.9-alpine`, zoals op Railway, 128 MB, leeg gestart).
- **Database overgezet**, structuur en data (`pg_dump | psql`): alle tien tabellen gelijk, met
  5000 spelers en 8 managers; geen gebruikers.
- App `footballfaceoff` uit de repo (Dockerfile, poort 3000, health-check `/health`, 512 MB),
  **auto-deploy uit**: een push mag hem niet stilletjes starten. Variabelen overgenomen, met
  `DATABASE_URL` en `REDIS_URL` naar de nieuwe containers.
  - Op Railway heette de variabele `"NODE_ENV "` (met spatie), dus de app zag hem niet. Hier
    `NODE_ENV=production`, zoals bedoeld.
- **De app bouwt niet**: `origin/main` (`c48cb07`, ook de laatste Railway-deploy) faalt in
  `tsc -b` op TypeScript-fouten en een ontbrekende `expo/tsconfig.base`. Dat verklaart
  waarschijnlijk waarom hij op Railway stilstaat. Lokaal staat `main` **13 commits voor** (niet
  gepusht, o.a. een deploy-checklist). Die zijn van Richard: pushen en dan handmatig deployen in
  Coolify. Niets aan de hand voor gebruikers: hij draaide al niet.
- Railway-project `Football-Faceoff` (Postgres en Redis kosten nog geld) mag op 2026-11-03 weg.

### 2026-10-28: schijf naar 320 GB

- Op verzoek van Richard, door zijn Claude in Chrome in de Hetzner Console: netjes afgesloten,
  rescale naar CPX42 met "CPU and RAM only" **uit**, Hetzner zette de server zelf weer aan.
  Een eerste blik met het vinkje aan gaf ten onrechte 160 GB als maximum voor elk type.
- De partitie groeide bij het opstarten vanzelf mee: `/` is 301 GB, 15 GB in gebruik (de
  build-cache was 's nachts door Coolify opgeruimd). Swap 4 GB nog actief, 8 vCPU, 16 GB.
- Alle containers gezond terug, ook Locus: `app.locuslegis.nl/api/health` 200 met
  `"database":"connected"`, plus elconteo, omgevingsbot en het Coolify-dashboard.
- Daarmee is de schijf geen blokkade meer voor `ocd`.

### 2026-10-28: ocd verhuisd

- **ocd-postgis** als Coolify-app (Dockerfile `ocd-postgis/`), niet als database-resource: die
  doet altijd `docker compose pull`, en een image dat alleen op de server bestaat, faalt dan.
  Auto-deploy uit, vaste containernaam `ocd-postgis`, volume op `/var/lib/postgresql/data`,
  `--shm-size=2g`, geheugenlimiet 6 GB, poort `5433:5432` (de Cloud Firewall laat 5433 niet
  door; van buiten getest: dicht). Coolify neemt `-p 127.0.0.1:…` uit de Docker-opties niet
  over. Instellingen via `ALTER SYSTEM`: `shared_buffers` 2 GB (Railway: 512 MB),
  `effective_cache_size` 8 GB, `work_mem` 32 MB, `maintenance_work_mem` 1 GB, `max_wal_size`
  8 GB, `random_page_cost` 1.1.
- **Overzetten** met `overzetten.sh` op de server, 10:04–11:36 (NL) voor alle data: v2a 19 min,
  wro 35 min, p2p 25 min; tempo ~10 MB/s, begrensd door de TCP-proxy van Railway.
- **Vertraging**: `pg_restore` ververste `p2p.naammatch_signaal` kaal, zonder
  `enable_bitmapscan = off`, en was na twee uur niet klaar. Afgebroken en met de instelling zelf
  gedaan: 6 min 51 s. `overzetten.sh` ververst de views nu zelf, na de rest van de post-data,
  met die instelling (getest op een post-data-dump: 519 items plus 15 views in de goede volgorde).
- **Getoetst**, via de SSH-tunnel: alle **147 tabellen exact gelijk** aan Railway. Van de 15
  views is `p2p.mv_regel_op_locatie` op Hetzner groter (392.743 tegen 383.403): vers
  berekend, op Railway na de laatste sync niet ververst. API-vergelijking: 9 van 11 gelijk; de
  twee `/v1/adres`-verschillen zijn die view (25 regels meer bij Broekhem 33) en de visies,
  die zonder zoektermen een `LIMIT 50` **zonder `ORDER BY`** hebben (main.py, rond regel 552):
  welke 50 je krijgt, hangt van de database af. Bestaand gedrag.
- **Snelheid**: op Railway gaven `/v1/adres` en `/v1/zoek` onder belasting 500 na 20–56 s; op
  Hetzner 0,2–3 s.
- **ollama-embed** als Coolify-app, vaste naam, volume voor de modellen, zelfde model-id.
  **ocd-api** als Coolify-app, auto-deploy aan (push naar main deployt, zoals op Railway),
  `DATABASE_URL` naar `ocd-postgis:5432`, `OLLAMA_URL` naar `ollama-embed:11434`.
- **DNS** `api.ponsenkaart.nl` (zone ponsenkaart.nl) van CNAME `hb9vvnlr.up.railway.app` naar
  A `2.31.94.124`, DNS only, door Richards Claude in Chrome. Certificaat van Let's Encrypt.
  ponsenkaart.nl en de omgevingsbot werken via de nieuwe server.
- **Tunnel voor de sync**: `dso-loader/scripts/prod_tunnel.sh` (lokaal 15433; 5433 is op
  Richards laptop bezet). `PROD_DB_URL` in `.env` wijst nu naar `127.0.0.1:15433`; de oude
  Railway-waarde staat er als commentaar. Droogloop: verbinding, `is_prod_url()` waar,
  parallelisme uit, Wro-blokkade zegt STOP. **Vóór de volgende sync `main` in de werkbranch
  mergen**: die mist `is_prod_url()`.

### Nog open na ocd

- **Railway `ocd`**: TCP-proxy uit en project opheffen na een week (2026-11-04), na akkoord.
  Tot dan deployt een push naar main óók nog op Railway.
- ~~GitHub Action van `instructieregels.nl`~~: gedaan op 2026-10-28, via de API (zie hieronder).
- **Verbeterpunten ocd-api** (geen verhuizing): visies in `/v1/adres` een vaste volgorde geven;
  `/v1/adres` met 12 zoektermen kost 17 s (veel korte query's na elkaar), de omgevingsbot stuurt
  er zoveel mee bij een schuurvraag.
- **Backups van ocd**: geen, bewust; zie V3 (besluit 2026-10-30).

### 2026-10-28, middag: viewers en instructieregels

- **Drie viewers liepen nog via Railway**: de Pages-proxy van OCD-viewer.nl,
  omgevingsdocumentenregister.nl en omgevingsvergunning-register.nl wees naar
  `ocd-api-production.up.railway.app`. Omgezet naar `https://api.ponsenkaart.nl` (`812e034`,
  `e81a4ca`, `8b0c62f`). De laatste twee deployen via Git; OCD-viewer is direct upload en is
  vanaf `main` gedeployd (live stond `13e3de9` = `main`). Gecontroleerd: hun `/api/health` komt
  op Hetzner binnen. `omgevingsvergunning-register.nl` stuurt door naar
  `omgevingsvergunningenregister.nl`.
- **instructieregels.nl: niet meer rechtstreeks bij de database.** Besluit Richard: route B,
  via de API, in plaats van een SSH-tunnel vanaf GitHub (route A). Zo blijft SSH op `apps-1`
  dicht en staat er geen databasewachtwoord in GitHub.
  - `ocd-api`: `GET /v1/instructieregels/monitor?gendate=…` (`b04ef31`), alleen met de private
    key, alleen-lezen transactie, eigen timeout van 180 s. De query is een kopie van
    `build/query.sql` in `ocd-api/queries/instructieregels_monitor.sql`; het endpoint stuurt de
    SHA-1 mee (`X-Query-Sha1`).
  - `instructieregels.nl` (`61e0f2d`): `build.sh` haalt de data bij de API en stopt als de
    SHA-1 afwijkt van zijn eigen `query.sql`. Workflow met `secrets.OCD_API_KEY`;
    `PSQL_CONN` verwijderd uit GitHub.
  - Getest: lokaal via de tunnel, op prod (17 s), en een echte run van de Action (56 s,
    groen, gedeployd). De uitvoer is tot op de byte zo groot als die van de run van 26-10 uit
    Railway (14.429.867 en 96.349 bytes).
  - De read-only rol `instructieregels_lezer` (alleen `core`, `irm`, `p2p`, `v2a`) is voor route
    A aangemaakt en nu ongebruikt. Laten staan of opruimen.
- **Werkbranch**: `main` gemerged in `feat/vergunning-regelkoppeling` (lokaal `ba21768`),
  runbook bijgewerkt voor de tunnel (in Richards niet-gecommitte wijzigingen).

### 2026-10-28, eind van de middag: de OCD-API krijgt zijn eigen naam

- De gedeelde OCD-API heette naar één van zijn gebruikers (`api.ponsenkaart.nl`). Nu
  **`ocd.elconteo.nl`** (besluit Richard): A-record in zone `elconteo.nl`, DNS only, eigen
  certificaat van Let's Encrypt. `api.ponsenkaart.nl` blijft als tweede naam werken.
- Omgezet naar `https://ocd.elconteo.nl`: de Pages-proxy's van ponsenkaart.nl, OCD-viewer.nl,
  omgevingsdocumentenregister.nl en omgevingsvergunningenregister.nl; de workflow van
  instructieregels.nl; `OCD_API_URL` van de omgevingsbot-backend in Coolify. Alles getest (200,
  Action groen, chatvraag 8 s).
- Let op: de branch `feat/normen-op-de-kaart` van OCD-viewer wijst nog naar het Railway-adres.
  Eerst `main` mergen voordat je daarvandaan deployt.
