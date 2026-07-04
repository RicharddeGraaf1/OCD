# Runbook — OCD-data bijwerken (incrementeel)

Doel: de OCD-database bijwerken **zonder te veel op te halen**. Elke laad-stap
registreert zichzelf in `core.load_run` (bron, scope, status, timestamp), zodat
je achteraf ziet wat wanneer is bijgewerkt (en het data-actualiteit-dashboard
dat toont). Zie `OCDviewer/docs/plans/data-health-dashboard.md`.

Draai commando's vanuit `c:/GIT/OCD/dso-loader` met de venv-python:
`.venv\Scripts\python -m src.cli <command>`.

## Volgorde (goedkoop → duur)

Niet elke bron kan incrementeel; het grain verschilt. Onderstaande volgorde
haalt zo min mogelijk op.

### 1. KOOP vergunningen — incrementeel op datum (echte skip)
`load-koop` slaat via `vth.etl_run` dagen over die al `status='ok'` zijn.
```
# van de dag ná de laatste geladen dag t/m vandaag
python -m src.cli load-koop --from <laatste+1> --to <vandaag>
python -m src.cli enrich-koop --loop        # verrijkt alleen inhoud_geladen_at IS NULL
```
Laatste geladen dag: `SELECT max(processed_date) FROM vth.etl_run WHERE status='ok';`

### 2. Toepasbare regels — alleen ontbrekende bronhouders
`--alle-ontbrekend` gebruikt de vlag `core.bronhouder.imtr_geladen`.
```
python -m src.cli load-imtr --alle-ontbrekend
```

### 3. Ozon regelingen — diff-gedreven per bronhouder
Eerst detecteren wat afwijkt t.o.v. DSO, dan alléén die bronhouders herladen.
```
python scripts/diff_dso_bronhouder_coverage.py --details --persist
# herlaad per bronhouder met n_mist > 0:
python -m src.cli load-api -o <code,naam>
```
Afgeweken bronhouders: `SELECT overheidscode, n_mist FROM core.bronhouder_dso_diff WHERE n_mist > 0 ORDER BY n_mist DESC;`

### 4. Besluitversies + ontwerpen — skippen zichzelf
Volledige API-scan, maar interne poort laadt alleen nieuwe/toekomstige items.
```
python -m src.cli wijziging besluiten
python -m src.cli wijziging ontwerpen
```

### 5. Afwijkvergunningen + planvoorraad — goedkoop / by-design vol
```
python -m src.cli load-ovg              # ~14,6k, idempotente upsert
python -m src.cli load-planvoorraad     # nieuwe snapshot (temporele meting)
```

### 6. Overslaan tenzij nodig
- `load-wro-teksten -g <cbs,...>` — alleen bij bekende plan-wijziging (ververst
  bestaande teksten niet: `ON CONFLICT DO NOTHING`).
- `load-wro-structuurvisies -n <G/P/R> -c <code>` — idem.
- `load-gemeentegrenzen` — alleen bij een gemeentelijke herindeling (TRUNCATE +
  full reload; ~1×/jaar).

## Correctheids-valkuil (belangrijk)

Geen enkele DSO-loader stuurt "gewijzigd sinds" naar de bron, en veel
kind-objecten staan op `ON CONFLICT DO NOTHING`. Gevolg: een **stil gewijzigde
bestaande** regeling/teksttekst wordt niet opgepikt, en verdwenen objecten niet
opgeruimd. → plan periodiek (bv. per kwartaal) een **volledige herlaad** voor de
zekerheid; incrementeel voor de tussenliggende slagen.

## Achteraf verifiëren

```
-- wat is wanneer bijgewerkt (laatste run per bron):
SELECT * FROM core.v_load_status ORDER BY bron;   -- (v_load_status: fase 3)
-- of ruw:
SELECT bron, scope, status, n_verwerkt, n_fout, finished_at
FROM core.load_run ORDER BY started_at DESC LIMIT 30;

-- mislukte / deels-runs:
SELECT bron, scope, error FROM core.load_run WHERE status IN ('gefaald','deels');

-- inhoudelijke gezondheid (bestaande data-health-laag):
REFRESH MATERIALIZED VIEW core.mv_bronhouder_health;
SELECT * FROM core.v_data_health;
```

## Wat `core.load_run` registreert

Eén rij per commando-run (batch-niveau). `scope` legt het grain vast:
`gm0344` / `pv25 [Omgevingsvisie]` / `2026-05-21..2026-07-04` / `alle-ontbrekend`
/ snapshotdatum / `alle`. Bij bronhouder-gescopete runs wordt
`core.bronhouder.laatst_geladen` bijgewerkt. `n_verwerkt` is nu gevuld voor
KOOP (`load-koop`/`enrich-koop`); voor de overige bronnen volgt dat later
(loaders geven nog geen telling terug) — status + timestamp + scope zijn er wel.
