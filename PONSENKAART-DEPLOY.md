# Ponsenkaart.nl — Productie-deploy plan

**Status:** ontwerp · **Strategie:** kostengevoelig, Cloudflare-first

## Beoogde architectuur

```
                       Cloudflare (gratis tier)
                              │
            ┌─────────────────┴───────────────────┐
            │ CDN + DDoS-protection + WAF        │
            ▼                                     ▼
    Cloudflare Pages                  Cloudflare proxy
    (statische HTML/JS/CSS)           (caching reverse proxy)
            │                                     │
            │ fetch                                │ proxy_pass
            │                                     ▼
            └─────────────────────────────► OCD-API @ Railway
                                                  │
                                                  ▼
                                         PostGIS @ Railway
                                                  ▲
                                                  │ wekelijkse refresh
                                          GitHub Actions cron
                                          (gratis tier)
```

Belangrijke ontwerpkeuze: **statische sites blijven van Railway weg**. Railway is voor de FastAPI + Postgres; alles wat statisch is gaat naar Cloudflare Pages. Wekelijkse data-refresh draait via GitHub Actions, niet via een Railway cron-service.

---

## Kostenraming

| Component | Kosten/maand | Schaalt met |
|---|---|---|
| OCD-API @ Railway (256-512 MB) | $5 | – |
| PostGIS @ Railway (5 GB, 1 GB RAM) | $10-15 | data-grootte |
| Ponsenkaart-frontend @ Cloudflare Pages | **gratis** | – |
| Cloudflare CDN/proxy van API | **gratis** | 100 GB egress/maand |
| GitHub Actions cron (wekelijks) | **gratis** | <2k min/maand |
| Plausible analytics (optioneel) | €9 | – |
| **Baseline zonder analytics** | **~$15-20/maand** | |

Spending-limit in Railway op **€40/maand** zetten als veiligheidsnet.

---

## Stap 0 — Repo's voorbereiden

```bash
cd c:/GIT/ponsenkaart.nl
git init
```

`.gitignore`:
```gitignore
.DS_Store
*.log
.env
.env.local
node_modules/
```

Push naar GitHub (private repo is prima — Cloudflare Pages werkt daarmee).

---

## Stap 1 — Domeinen bij Hostnet

Drie te registreren:
- `ponsenkaart.nl`
- `omgevingsvergunningenregister.nl` (let op: nieuwe naam, was `omgevingsvergunning-register.nl`)
- `ocd-viewer.nl`

Setup per domein:
1. Registreer bij Hostnet (€0,10 eerste jaar, daarna ~€10/jaar)
2. Activeer Cloudflare nameservers in plaats van Hostnet-DNS (binnen Cloudflare gratis tier)
3. Hostnet update: zet nameservers naar bv. `lara.ns.cloudflare.com` + `walt.ns.cloudflare.com`
4. Wacht op DNS-propagatie (~1 uur)

**Tip**: zet de Cloudflare-DNS-TTL op 5 minuten tijdens deploy-iteraties, later omhoog naar 1 uur.

---

## Stap 2 — OCD-API klaar voor productie

### Twee-keys-strategie

Al ingebouwd in [`main.py`](c:/GIT/OCD/ocd-api/main.py) (2026-05-23):

```bash
# Railway → OCD-API service → Variables:
OCD_API_KEY_PUBLIC=<random 32-char string, in browser-HTML>
OCD_API_KEY_PRIVATE=<random 32-char string, voor backend-clients>
```

Genereer beide keys met `openssl rand -base64 32`. Verstuur `_PRIVATE` via 1Password/Bitwarden, **niet via email/Slack**. Bij scraper-misbruik op `_PUBLIC`: invalideer en deel een nieuwe, zonder backend-clients te raken.

### Cache-headers

Al ingebouwd in [`ponsenkaart.py`](c:/GIT/OCD/ocd-api/ponsenkaart.py):
- `Cache-Control: public, max-age=3600, s-maxage=86400` op alle 3 ponsenkaart-endpoints
- Browser cachet 1 uur, Cloudflare 24 uur
- Matview refresht wekelijks → cache 24 uur is veilig

Voor andere endpoints (vergunningen-router) zou je een gevarieerd schema willen:

| Endpoint-soort | Cache-Control |
|---|---|
| Stats / aggregaten | `public, max-age=300, s-maxage=300` (5 min) |
| Lijst met populaire filter-combinaties | `public, max-age=60, s-maxage=60` |
| Bbox-gebaseerde queries | `private, max-age=30` (geen CDN-cache) |
| Detail-records | `public, max-age=3600, s-maxage=86400` |

**Realisme over caching voor vergunningenregister**: de bbox/filter long-tail betekent ~30-40% hit-rate i.p.v. ponsenkaarts 95%+. Maar Cloudflare blijft nuttig vanwege DDoS-protectie, IP-rate-limiting, en HTTPS-termination — ook bij lage hit-rates is de overhead ~5ms per proxy-pass.

### CORS

Al voorbereid in [`main.py`](c:/GIT/OCD/ocd-api/main.py). Bij domein-wijziging van vergunningen-register naar nieuwe naam moet de allowlist mee:

```python
allow_origins=[
    # dev
    "http://localhost:8766",   # ponsenkaart lokaal
    "http://localhost:8765",   # vergunningen-register lokaal
    # productie
    "https://ponsenkaart.nl",
    "https://omgevingsvergunningenregister.nl",
    "https://ocd-viewer.nl",   # zodra die live is
],
```

---

## Stap 3 — OCD-API + Postgres deploy

Volg [c:/GIT/OCD/dso-loader/DEPLOY.md](c:/GIT/OCD/dso-loader/DEPLOY.md) — die heeft de complete walkthrough voor Railway PostGIS 17 + FastAPI service + dump/restore.

Vergeet niet:
- `OCD_API_KEY_PUBLIC` en `OCD_API_KEY_PRIVATE` in Railway-variables
- DATABASE_URL automatisch gelinkt via Railway service-koppeling
- Public Networking voor dump-restore, daarna **uit** voor de DB

---

## Stap 4 — Cloudflare in front van de API

1. Cloudflare DNS: voeg `CNAME api → <railway-app>.up.railway.app`, **proxy aan** (oranje wolkje)
2. Cloudflare DNS-record voor je hoofddomein: bv `api.ponsenkaart.nl` of een gedeeld `ocd-api.ocd-viewer.nl`
3. In Cloudflare → Rules → Cache Rules: vertrouw `Cache-Control` headers (default doet dit al)
4. Cloudflare → Security → Rate Limiting Rules: limit `path contains "/v1/"` op 100 requests/min per IP

Resultaat: de API achter `https://api.ponsenkaart.nl` (of `ocd-api.ocd-viewer.nl`) met gratis caching + DDoS-protectie.

### Cache-purge bij data-refresh

Na een wekelijkse OCD-refresh wil je Cloudflare's cache evict zodat verse data zichtbaar wordt. Twee opties:

- **Doe niets**: cache verloopt vanzelf na 24h (`s-maxage=86400`). Acceptabel voor wekelijkse refresh.
- **Trigger Cloudflare API-purge** in de GitHub Actions cron (`curl -X POST` op `https://api.cloudflare.com/client/v4/zones/.../purge_cache`). 30 seconden extra werk.

Aanbeveling: niets doen. Bezoekers zien data dan max 24 uur achter. Refresh draait om 03:00 UTC; om 04:00 begint de cache te lopen leeg te lopen; rond 12:00 'sochtends zit iedereen op verse data.

---

## Stap 5 — Frontend op Cloudflare Pages

### Repo connecten

1. Cloudflare Dashboard → Workers & Pages → Create application → Pages → Connect to Git
2. Selecteer je GitHub-repo `ponsenkaart.nl`
3. Build configuratie: **leeg** (geen build-command, geen output-dir behalve `/`)
4. Build-output: laat staan op `/`

Cloudflare ziet `Ponsenkaart.html` + de assets en serveert ze direct.

### Custom domain

In Pages-project → Custom domains → `ponsenkaart.nl` + `www.ponsenkaart.nl`. Cloudflare regelt automatisch:
- DNS-records (omdat domain via Cloudflare gemanaged is)
- SSL/TLS-certificaat (Universal SSL, gratis, auto-renew)

### Productie API-URL

In `Ponsenkaart.html` config-blok aanpassen vóór commit:

```html
<script>
  window.OCD_API_BASE = location.hostname === 'localhost'
    ? 'http://localhost:8001'
    : 'https://api.ponsenkaart.nl';
  window.OCD_API_KEY = location.hostname === 'localhost'
    ? ''
    : '<PUBLIC_KEY_HIER>';
</script>
```

Yes, de public-key staat dan in de HTML. Dat is bedoeld — het is een rate-limiting-handle, geen secret. Bij misbruik invalideer je 'm en push je een nieuwe versie van de HTML met een nieuwe key (Cloudflare Pages re-deployt automatisch op git push).

### `www.` → bare canoniek redirect

Cloudflare → Rules → Bulk Redirects:
- Source: `https://www.ponsenkaart.nl/*`
- Target: `https://ponsenkaart.nl/$1`
- Status: 301 (permanent)

---

## Stap 6 — Wekelijkse data-refresh via GitHub Actions

### Voor nu (handmatig)

Maak `c:/GIT/OCD/dso-loader/Makefile`:

```make
.PHONY: refresh-ponsenkaart

refresh-ponsenkaart:
	python -m src.cli refresh-ponsenkaart-stats
	@echo "Done. Volgende run aanbevolen: maandag $$(date -d '+7 days' '+%Y-%m-%d')"
```

Draai handmatig na elke OW-data-ingest of wekelijks.

### Voor later (cron via GitHub Actions)

In je dso-loader repo, voeg toe `.github/workflows/refresh-ponsenkaart.yml`:

```yaml
name: Refresh ponsenkaart-stats
on:
  schedule:
    - cron: '0 3 * * 1'  # maandag 03:00 UTC
  workflow_dispatch:      # ook handmatig triggerbaar

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: pip install -e .
      - run: python -m src.cli refresh-ponsenkaart-stats
        env:
          DATABASE_URL: ${{ secrets.RAILWAY_DATABASE_URL }}
```

`RAILWAY_DATABASE_URL` is een secret in GitHub repo-settings — Railway moet hiervoor **Public Networking** aanstaan op de Postgres. Of: gebruik een Railway-token + `railway run` in de workflow.

Schat 30-60 seconden runtime per refresh × 4 per maand = 4 minuten/maand → ruim binnen GitHub Actions gratis-tier (2000 min/mo voor private repo, unlimited voor public).

---

## Stap 7 — Analytics

**Aanbeveling: Plausible Cloud (€9/maand)**

- Privacy-vriendelijk (geen cookies, geen GDPR-banner nodig)
- Eén script-tag in `Ponsenkaart.html`
- Dashboard via plausible.io
- Schaalt naar 10k+ page-views per maand zonder bijbetalen

Goedkoper alternatief: Umami self-host op Railway (~$3/mo extra) — maar weer een container om te beheren. Plausible is dat extra waard.

Geen analytics is ook prima — past bij open data / civic-tech-vibe. Beslis je later.

Script-tag (Plausible):
```html
<script defer data-domain="ponsenkaart.nl" src="https://plausible.io/js/script.js"></script>
```

---

## Stap 8 — Smoke-test productie

```bash
# DNS / HTTPS
curl -I https://ponsenkaart.nl
curl -I https://api.ponsenkaart.nl/v1/ponsenkaart/stats -H "X-Api-Key: <PUBLIC>"

# Cache-header zichtbaar
curl -I https://api.ponsenkaart.nl/v1/ponsenkaart/stats -H "X-Api-Key: <PUBLIC>" \
  | grep -i cache-control
# → cache-control: public, max-age=3600, s-maxage=86400

# Cloudflare cache-hit (de tweede keer)
curl -I https://api.ponsenkaart.nl/v1/ponsenkaart/stats -H "X-Api-Key: <PUBLIC>" \
  | grep -i cf-cache-status
# → cf-cache-status: HIT (na eerste call)
```

In browser DevTools (Network tab):
- `index.html` 200 van Cloudflare Pages
- `gemeenten` 200, met `cf-cache-status: HIT` na een paar pagina-reloads
- `ponsen` idem
- 0 CORS-errors

---

## Te beslissen vóór deploy-dag

- [x] Endpoints met of zonder key? → **Met key**, twee-tier (public/private)
- [x] www-redirect? → **Bare canoniek, www. → bare**
- [x] Refresh-cadans? → **Handmatig nu, GitHub Actions cron voor wekelijks**
- [ ] Analytics? → Plausible aanbevolen, jij beslist
- [ ] Cloudflare zone-naam voor API? → `api.ponsenkaart.nl` of gedeeld `ocd-api.ocd-viewer.nl`?
  - Aanbeveling: gedeeld op `ocd-api.ocd-viewer.nl` — één API serveert alle drie de viewers; net domeinhygiëne.

---

## Checklist deploy-dag

- [ ] Domeinen geregistreerd bij Hostnet, nameservers → Cloudflare
- [ ] Cloudflare-account aangemaakt, drie zones toegevoegd
- [ ] GitHub-repo's gepushed: `ponsenkaart.nl`, evt. `omgevingsvergunningenregister.nl`
- [ ] Railway: OCD-API en Postgres draaien (zie [c:/GIT/OCD/dso-loader/DEPLOY.md](c:/GIT/OCD/dso-loader/DEPLOY.md))
- [ ] Railway environment-variabelen: `OCD_API_KEY_PUBLIC` + `OCD_API_KEY_PRIVATE` gezet
- [ ] Productie-OCD bevat `core.gemeentegrens` + `v2a.ponsenkaart_gemeente_stats` (`python -m src.cli load-gemeentegrenzen` + `refresh-ponsenkaart-stats` + `repair-pons-placeholders`)
- [ ] CORS allowlist in `main.py` aangevuld met productie-domeinen
- [ ] Cloudflare DNS: `api.<domein>` → Railway, proxy aan (oranje wolkje)
- [ ] Cloudflare rate-limiting rule op `/v1/*`
- [ ] Cloudflare Pages: ponsenkaart-repo verbonden, custom domain `ponsenkaart.nl` + `www.`
- [ ] Productie-URL en publieke key in HTML config-blok
- [ ] `www.` bulk-redirect aan in Cloudflare
- [ ] Smoke-test: alle drie endpoints leveren data, cf-cache-status HIT na refresh
- [ ] Spending-limit Railway op €40/maand
