# Prod-migratie — sessie 2026-07 (data-actualiteit + regelingen-fixes + IMRO2006)

> Status: Open · Datum: 2026-07-05
> Aanvulling op `dso-loader/DEPLOY.md` + `restore-dev-naar-prod.ps1` +
> `docs/prod-restore-en-hosting-afweging.md`. Beschrijft alleen de **delta** van
> deze sessie; alle wijzigingen zijn getest op dev.

Drie stukken, oplopende moeilijkheid: **schema (makkelijk) → data (de echte
keuze) → code-deploy (bekend maar fiddly)**. Volgorde: schema vóór data-views,
code kan gelijk met schema.

---

## 1. Schema/DDL — idempotent, makkelijk

Draai op de prod-DB (via de Railway TCP-proxy; zie DEPLOY.md) deze migratie-scripts:

| Script | Objecten |
|---|---|
| `dso-loader/scripts/2026-07-add-load-run.sql` | `core.load_run` (+index), `core.v_load_status`, `core.bron_totaal()`, `core.v_bron_totalen` |
| `dso-loader/scripts/2026-07-add-geometrie-herkomst.sql` | `wro.ruimtelijk_instrument.geometrie_herkomst` |
| `dso-loader/scripts/2026-07-add-gemeentegrens-historisch.sql` | `core.gemeentegrens_historisch` (+GIST) |

Alle drie `IF NOT EXISTS` / `CREATE OR REPLACE` → veilig herhaalbaar. Toepassen bv.:
```
PYTHONPATH=. python -c "from src.db import get_conn, execute_sql_file; \
  execute_sql_file(get_conn(), open('scripts/<script>.sql',encoding='utf-8').read())"
```
(met de loader-`.env` tijdelijk op prod, zie §2).

**Prod-valkuil, al afgevangen:** `bron_totaal()` bevraagt o.a. `p2pwijziging.besluit`.
`p2pwijziging` is bij de restore van 2026-06-10 **uitgesloten**. De functie is
prod-veilig (dynamische EXECUTE → `NULL` bij `undefined_table`), dus geen crash;
`ozon-besluitversies`/`ozon-ontwerpen` tonen dan alleen NULL-totaal. Idem als
`wro`/`i2a`-tabellen ontbreken.

De backfill in `2026-07-add-load-run.sql` (uit `p2p.regeling_load` + `vth.etl_run`)
draait alleen als die tabellen data hebben; anders geen rijen (geen fout).

---

## 2. Data — de echte keuze

Prod is per 2026-06-10 volledig gerestored vanaf dev, dus de te-migreren data is de
**delta van deze sessie**: 173 regeling-expressies + annotatie-herkoppelingen
(p2p), 3.736 IMRO2006-plannen + teksten + historische grenzen (wro + core),
853k KOOP-inhoud (vth), en `core.bronhouder.laatst_geladen` + `core.load_run`.

Twee wegen — **kies bewust**:

**A. Loaders opnieuw draaien tegen prod** (loader-`.env` → prod-DB)
- `load-koop --from <laatste etl_run+1> --to vandaag` + `enrich-koop --loop`
- `load-regelingen-diff` (expressie-diff, gericht) + `herlaad-annotaties`
- `load-imtr --alle-ontbrekend`, `wijziging besluiten/ontwerpen`, `load-ovg`,
  `load-planvoorraad`
- `load-gemeentegrens-historisch` + `load-wro-imro2006`
- **Pro:** reuse van geteste, idempotente loaders; geen grote dump. **Con:** zwaar
  (enrich 853k + IMTR 425 + IMRO2006 3,7k IHR-calls opnieuw); externe API-belasting;
  vereist prod-baseline gelijk aan de dev-basis vóór deze sessie.

**B. Data-sync dev→prod** (her-dump van de geraakte schema's via het bestaande runbook)
- `restore-dev-naar-prod.ps1` opnieuw, gericht op de geraakte schema's
  (p2p, wro, i2a, vth-delta, core).
- **Pro:** exact de dev-staat, geen her-fetch. **Con:** groot; alle bekende
  restore-valkuilen (search_path, parallelle REFRESH op kleine /dev/shm,
  vector/tiger ontbreekt op de image, memory-cap).

**Aanbeveling:** voor de **regelingen + IMRO2006** is **A** aantrekkelijk (diff-gedreven,
idempotent, relatief klein). Voor de **KOOP-inhoud (853k)** is her-enrichen tegen de
rate-limit onaantrekkelijk → daar past **B** (of accepteren dat prod de inhoud later
inhaalt). Dus een **hybride**: A voor regelingen/IMRO2006/wijzigingen, B (of uitstel)
voor de KOOP-enrich-delta. Leg de gekozen route vast vóór uitvoering.

Let op: de IMRO2006-loader vereist een **verse planvoorraad-snapshot** op prod
(`load-planvoorraad`) én `IHR_API_KEY` in de prod-loader-omgeving.

---

## 3. Code-deploy — bekend, fiddly

- **ocd-api** (`main.py`: `/v1/load-status`, `totalen`, `frbr_work`-dedup,
  `geometrie_herkomst` in Wro-response): `railway up` **vanaf repo-root** met
  service-root `ocd-api` (niet vanuit `ocd-api/`). GitHub-auto-deploy staat uit →
  handmatig. Na deploy op een verse prod-DB: check de **search_path-gotcha**
  (`ALTER DATABASE railway SET search_path=...` + redeploy) zodat ongekwalificeerde
  functies (`ocd_artikel_label`) werken.
- **dso-loader**: wordt niet "gedeployed" — draait waar je 'm aanroept (lokaal/cron).
- **viewer** (`/data`-pagina, IMRO2006-badge, dedup-afhankelijkheid): deploy is nog
  aspirationeel (Dockerfile-plan). De frontend-wijzigingen werken zodra de viewer
  wél live gaat; geen aparte actie nu.

---

## Volgorde + verificatie

1. Schema-scripts (§1) op prod.
2. Code-deploy ocd-api (§3) — of gelijktijdig; endpoints hebben de views nodig.
3. Data (§2, gekozen route).
4. Verifiëren:
   - `GET /v1/load-status` → 200, `bronnen`/`totalen` gevuld.
   - `GET /v1/data-health` → 200.
   - `SELECT count(*) FROM wro.ruimtelijk_instrument WHERE geometrie_herkomst='ambtsgebied-imro2006';`
   - Een `/v1/viewer/regelingen` op een punt in een geladen gemeente → Wro-plan met
     `geometrie_herkomst` gevuld.
5. Fase 5 (cron) apart: nachtelijke `diff` + `REFRESH mv_bronhouder_health` +
   `REFRESH v2a.ponsenkaart_gemeente_stats`.
