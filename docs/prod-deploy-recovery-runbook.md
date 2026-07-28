# OCD-API prod — diagnose & herstel-runbook ("Application not found")

> Opgesteld 2026-07-28 n.a.v. de prod-outage waarbij `api.ponsenkaart.nl` **én**
> het Railway-domein `*.up.railway.app` allebei `{"message":"Application not found"}`
> teruggeven. Railway-ops kunnen **niet** vanuit de agent-sessie (CLI-login pakt de
> MCP-plugin niet op; TCP-proxy/volume/domains zijn dashboard-only) → draai dit zelf
> in een terminal waar `railway` is ingelogd.

## ✅ Vastgestelde oorzaak van dit incident (2026-07-28)

**De app was niet plat — de custom domain was losgeraakt.** Via de CLI bleek:

- `railway status` → service **`ocd-api` ● Online** op `https://ocd-api-production.up.railway.app`.
- `curl https://ocd-api-production.up.railway.app/health` → `200 {"status":"ok"}`
  (`/v1/data-health` → `403` = auth-vereist, dus fail-closed werkt correct).
- `railway domain -s ocd-api` → toont **alléén** het Railway-domein; `api.ponsenkaart.nl`
  hangt **niet** meer aan de service.
- DNS: `api.ponsenkaart.nl` = CNAME naar een **stale** target (`hgdb9fyl.up.railway.app`)
  dat geen service meer claimt → Railway-edge geeft "Application not found".

De onderstaande "deployment serveert niet"-hypotheses waren dus NIET van toepassing
in dit incident — het was zuiver de domeinkoppeling. Diagnose-les: **test altijd
eerst het service-eigen `*.up.railway.app`-domein** (uit `railway status`), niet het
domein waar de custom-DNS heen wijst — anders diagnostiseer je de verkeerde laag.

**Fix (2 stappen):**
1. Railway (schrijven vereist een verse `railway login`): `railway domain api.ponsenkaart.nl -s ocd-api`
   — of dashboard → ocd-api → Settings → Networking → Custom Domain. Railway geeft een CNAME-target.
2. Cloudflare DNS: record `api` (CNAME) → dat nieuwe target (vervang het stale `hgdb9fyl…`),
   op **DNS only** (grijze wolk). Wacht op propagatie + cert → `curl https://api.ponsenkaart.nl/health`.

> Let op: de `railway` CLI werkt read-only vanuit een agent-Bash-tool (deelt het login-token
> op de machine), maar **schrijf-acties** (domain add) gaven `Unauthorized → railway login again`
> → die moet je zelf in je terminal doen.

## Symptoom & wat het betekent

`{"status":"error","code":404,"message":"Application not found"}` is de **Railway
edge-router**, niet FastAPI. Het betekent: de router heeft **geen actieve deployment**
om het verzoek naartoe te sturen voor die hostname. Omdat het op **beide** domeinen
gebeurt (custom + `*.up.railway.app`) is het géén domain-mapping-probleem meer maar
de **deployment serveert niet**.

Ter contrast — als de app wél draaide maar een route miste, kreeg je FastAPI's
`{"detail":"Not Found"}`. Dat is hier niet het geval.

## Meest waarschijnlijke oorzaken (geordend)

1. **Crash-loop uitgeput.** `railway.toml` heeft `restartPolicyMaxRetries = 10` +
   healthcheck `/health`. Faalt de container 10× bij boot → Railway markeert de
   deploy failed → edge geeft "Application not found".
2. **Fail-closed auth of DB.** De container **weigert te starten** als
   `OCD_REQUIRE_AUTH=true` en `OCD_API_KEY_PUBLIC/PRIVATE` of `DATABASE_URL`
   ontbreken/kapot zijn (bv. de `${{Postgres.DATABASE_URL}}`-reference brak). →
   crash-loop → oorzaak 1.
3. **PostGIS-service plat.** `/health` raakt de DB; ligt PostGIS eruit (OOM onder
   zware REFRESH — geheugencap staat op 2 GB — of gestopt), dan faalt de healthcheck
   → crash-loop. Check de PostGIS-service apart.
4. **Service/deploy verwijderd of naar andere service gedeployed**, waardoor het
   domein naar een service zonder actieve deploy wijst.

## Stap 1 — Diagnose (volgorde + interpretatie)

```bash
railway whoami                 # ingelogd?
railway link                   # kies project 'ocd' + environment 'production' (interactief)
railway status                 # bevestig gelinkte project/env/service

# Kies de API-service en bekijk de laatste deploy:
railway service                # selecteer 'ocd-api' (of hoe de service heet)
railway logs                   # RUNTIME-logs actieve deploy — zoek crash / DB-fout / "refused to start"
railway logs --build           # laatste BUILD-logs — build gefaald?

railway domain                 # welke domeinen hangen aan DEZE service? staat api.ponsenkaart.nl + een *.up.railway.app hier?
railway variables              # staan DATABASE_URL, OCD_REQUIRE_AUTH, OCD_API_KEY_PUBLIC/PRIVATE er nog?
```

Ook in het **dashboard** (CLI toont dit niet altijd):
- Service `ocd-api` → tab **Deployments**: is de laatste **Crashed/Removed/Failed**?
- Service **PostGIS 17**: staat die op **Running**? Geheugen/restarts?

**Interpretatie:**
- `railway logs` leeg / "no active deployment" → deploy is dood → **Stap 2**.
- Build failed in `--build` → fix build, dan **Stap 2**.
- Log toont DB-connectiefout of "refused to start" → oorzaak 2/3: fix env-var of
  herstart PostGIS, dan **Stap 2**.
- `railway domain` mist het domein op de service → domein opnieuw koppelen (**Stap 4**).

## Stap 2 — Herstel / redeploy

> ⚠️ **`railway up` uploadt de working tree** (prod draait historisch via `railway up`
> vanaf feature-branches, niet via git-push). De OCD-working-tree heeft nu veel
> ongecommit in-flight werk — deploy vanaf een **bekend-goede staat** (stash/checkout
> wat niet mee moet), anders sleep je half werk mee naar prod.

> ⚠️ **Deploy vanaf de REPO-ROOT `c:/GIT/OCD`, NIET vanuit `ocd-api/`.** De service-root
> staat op `ocd-api` (`railway.toml` daar); `railway up` vanuit `ocd-api/` geeft
> `Build Failed: lstat …/ocd-api: no such file`. (DEPLOY.md stap "cd ocd-api" is
> op dit punt achterhaald.)

```bash
# 0. Zorg dat PostGIS eerst Running is (dashboard) — de API healthcheck heeft 'm nodig.
# 1. Vanaf repo-root, met de juiste service gelinkt:
cd /c/GIT/OCD
railway up                     # bouwt via Dockerfile, start uvicorn main:app op $PORT
# 2. Volg de deploy:
railway logs --build           # wacht op "build succeeded"
railway logs                   # wacht tot uvicorn luistert + healthcheck /health slaagt
```

Als oorzaak 2 (env-vars): zet ze eerst terug (zie `ocd-api/PRODUCTION-CHECKLIST.md §1`):
`OCD_REQUIRE_AUTH`, `OCD_API_KEY_PUBLIC`, `OCD_API_KEY_PRIVATE`, `DATABASE_URL`
(→ `ocd_reader`), `OCD_ENABLE_DOCS=false`.

## Stap 3 — Verificatie (van binnen naar buiten)

```bash
# a) Railway-domein direct (sluit domain-mapping uit als variabele):
curl https://<service>.up.railway.app/health          # -> {"status":"ok"}
curl https://<service>.up.railway.app/v1/data-health  # -> JSON, geen "Application not found"
# b) Custom domein:
curl https://api.ponsenkaart.nl/health                # -> {"status":"ok"}
# c) Bot-datapad (de reden van dit hele traject):
curl "https://api.ponsenkaart.nl/v1/adres?...&apikey=..."   # 'inhoud' bevat rauwe STOP-XML
```

## Stap 4 — Custom domein herstellen (alleen als Railway-domein wél werkt maar custom niet)

- Dashboard → service `ocd-api` → **Settings → Domains** → `api.ponsenkaart.nl`
  toegevoegd? Zo niet: toevoegen; Railway geeft een CNAME-target.
- DNS (Cloudflare): `api` CNAME → het door Railway opgegeven `*.up.railway.app`-target.
  Let op: het Railway-target kan gewijzigd zijn t.o.v. een oude waarde
  (`ocd-api-production…` vs `hgdb9fyl…`). Zet de CNAME op het **huidige** target.
- Cloudflare: zet de record op **DNS only** (grijze wolk) of controleer dat de
  proxy-modus geen extra laag 404't.

## Nazorg (aanbevolen)

- **UptimeRobot** op `/health` (5-min, gratis) — dan zie je zo'n outage meteen
  (staat al als open punt in de checklist).
- Overweeg `restartPolicyMaxRetries` te verhogen of een alert op deploy-failure,
  zodat een tijdelijke DB-hik de deploy niet permanent doodt.
- Na herstel: de bot-verificatie (gestructureerde bronweergave / geluidtabel) kan
  pas draaien als dit pad weer 200 geeft.
```
