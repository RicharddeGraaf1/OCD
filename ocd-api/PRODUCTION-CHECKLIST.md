# OCD API — Productie-checklist

Volgorde: **blockers → security → deploy-config → monitoring → nice-to-have**.
Vink af bij voltooiing. Items met "(door agent gefikst)" zitten in deze commit.

---

## 0. Blockers — moeten vóór `railway up`

- [ ] **Commit `ponsenkaart.py` en `vergunningen.py`** (nu untracked).
  `main.py` importeert ze; Railway-deploy via GitHub crasht zonder.
- [ ] **Commit & push lokale wijzigingen** (`main.py`, `keywords.py`,
  `test_viewer.py`, `README.md` zijn modified, 4 commits ahead van `origin/main`).
- [ ] **Railway-PostGIS database aangemaakt** (1-click template, zie
  [../dso-loader/DEPLOY.md](../dso-loader/DEPLOY.md) stap 1).
- [ ] **Database-dump gerestored** naar Railway (`pg_dump` + `pg_restore`,
  ~5 GB, 30-60 min via TCP-proxy).
- [ ] **Ponsenkaart-bootstrap gedraaid** na restore: `load-gemeentegrenzen`,
  `repair-pons-placeholders`, `refresh-ponsenkaart-stats`.

## 1. Security — vóór go-live

- [x] **Fail-closed API-key auth** (door agent gefikst).
  Zet in Railway: `OCD_REQUIRE_AUTH=true`. Container weigert te starten
  als die env-var aan staat en er geen `OCD_API_KEY_*` zijn geconfigureerd.
- [ ] **Railway env-vars zetten**:
  - `OCD_REQUIRE_AUTH=true`
  - `OCD_API_KEY_PUBLIC=<random 32-char>` (in viewer-HTML)
  - `OCD_API_KEY_PRIVATE=<random 32-char>` (backend-only, bv. Omgevingsbot)
  - `DATABASE_URL=<read-only-user, zie 1.3>`
  - `OCD_ENABLE_DOCS=false` (Swagger uit in productie)
- [x] **Rate limiting** via `slowapi` (door agent gefikst).
  Per-tier limits: `public` 60/min, `private` 600/min, unauth (lokaal) 30/min.
  Defaults overrideable via env-vars `OCD_RATE_PUBLIC`, `OCD_RATE_PRIVATE`.
- [ ] **Read-only DB-user aanmaken op Railway**.
  SQL staat in [db-readonly-user.sql](db-readonly-user.sql). Draai éénmalig
  als `postgres` op de Railway-DB, daarna `DATABASE_URL` switchen naar
  `ocd_reader`.
- [x] **Statement timeout 10s op de pool** (door agent gefikst, [db.py](db.py)).
  Voorkomt dat één slecht ST_Intersects de hele pool leegtrekt.
- [x] **Structured logging incl. API-tier + endpoint** (door agent gefikst).
  Per request: tier (`public`/`private`/`legacy`/`anonymous`), path, status.
  Bij scraper-misbruik kun je zien welke tier lekt.
- [x] **Swagger `/docs` configurable** (door agent gefikst).
  Zet `OCD_ENABLE_DOCS=false` in Railway. Schema blijft intern bereikbaar
  via `/openapi.json` als je dat wilt — anders set ook `OCD_ENABLE_OPENAPI=false`.
- [ ] **Sentry (of equivalent) voor error-tracking**.
  `pip install sentry-sdk[fastapi]`, env-var `SENTRY_DSN`.
  Optioneel — als je geen tijd hebt: Railway logs zelf zijn voldoende voor v1.
- [ ] **CORS-domeinen verifiëren**: productie-origins staan al in
  [main.py:71-76](main.py#L71-L76). Check dat Cloudflare Pages exact die
  origin stuurt (`https://ponsenkaart.nl`, niet `www.` of `http`).

## 2. Deploy-config

- [x] **`railway.toml` aangemaakt** (door agent gefikst).
  Dockerfile-builder + healthcheck op `/health`.
- [x] **`Dockerfile` bestaat** — geen actie.
- [ ] **`PORT` env-var**: Railway zet die automatisch; Dockerfile honoreert
  `${PORT:-8001}`.
- [ ] **`DATABASE_URL` linken** aan de PostGIS-service in Railway
  (Project → Variables → Reference → `${{Postgres.DATABASE_URL}}`).
  Gebruik daarna *Variables* om die naar `ocd_reader` te overschrijven.
- [ ] **Health check werkt**: `curl https://<app>.up.railway.app/health`
  geeft `{"status":"ok"}`.

## 3. Monitoring & ops

- [ ] **Uptime-monitor** op `/health` (UptimeRobot, BetterStack, Cronitor).
  5 min interval = gratis. Stuurt mail bij downtime.
- [ ] **Log-aggregatie**: Railway logs zijn 7 dagen retentie. Voor langere
  retentie: Logflare, Axiom, of Better Stack Logs (gratis tier ~1 GB).
- [ ] **Refresh-cadans vastleggen**:
  - DSO-data via [dso-loader](../dso-loader/) (handmatig of cron — TBD)
  - `v2a.ponsenkaart_gemeente_stats` matview wekelijks via GitHub Actions
    cron (zie `ponsenkaart.nl/DEPLOY.md` stap 6)
  - `core.gemeentegrens` jaarlijks (gemeente-herindelingen)
- [ ] **Backup-schedule**: Railway maakt automatisch snapshots; check
  retentie in dashboard. Voor extra zekerheid: `pg_dump` naar S3/R2
  wekelijks.

## 4. Nice-to-have (kan ook na v1)

- [ ] **PDOK-call resilience**: `httpx` retries voor de adres-lookup in
  `/v1/adres`. Nu crasht endpoint als PDOK 502 geeft.
- [ ] **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`. Voor een JSON-only API marginal nut, maar 5-regels
  middleware.
- [ ] **API-versie pinnen**: `/v1/` is al gebruikelijk; v2 plannen voor
  breaking changes (verwijderingen, hernaamingen).
- [ ] **Per-key quota / billing-tier**: als de API later commercieel wordt.
- [ ] **OCD.md project-dashboard entry** (`c:\GIT\ProjectenDashboard\projecten\OCD.md`)
  — er is wel `OCDviewer.md`, maar geen entry voor de API/backend zelf.

---

## Volgorde van uitvoeren (aanbevolen)

1. **Lokaal**: untracked files committen, push naar `origin/main`.
2. **Railway**: PostGIS-service aanmaken, dump + restore.
3. **Railway**: read-only user aanmaken via psql/Railway-CLI met
   [db-readonly-user.sql](db-readonly-user.sql).
4. **Railway**: env-vars zetten (zie 1.2).
5. **Railway**: FastAPI-service deployen (`railway up` of via GitHub-trigger).
6. **Smoke test**: `curl /health` + één geauthenticeerde call.
7. **Bootstrap**: `load-gemeentegrenzen` + `refresh-ponsenkaart-stats`
   tegen Railway DB (DATABASE_URL in lokale `.env` tijdelijk wijzen
   naar Railway, daarna terug naar localhost).
8. **Frontends**: viewer-HTML aanpassen om `X-Api-Key: <PUBLIC_KEY>` mee
   te sturen + base-URL naar Railway-domein.
9. **Monitoring**: UptimeRobot toevoegen.
