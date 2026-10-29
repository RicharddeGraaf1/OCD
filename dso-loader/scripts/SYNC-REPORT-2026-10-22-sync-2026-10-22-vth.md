# Sync-rapport 2026-10-22

**Duur:** 1.9 uur · **fouten:** 0 · run_id 56
## Uitgangssituatie
- DB-grootte vooraf: 98 GB
- regelingen vooraf: 2133
- ALA-rijen vooraf: 404057
- schijf vrij: 391 GB

## Snapshot & dedup
- run_id: 56 (label `sync-2026-10-22-vth`)
- regeling_load-snapshot: 2133 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## vth (vergunningen)
- KOOP-kennisgevingen 2026-10-17..2026-10-22: load ok (geometrie-backfill draait in post)
- enrich-koop: ok · 68.1 min · +5,825 kennisgevingen · 1.4 kennisgevingen/s
- DSO-afwijkvergunningen (BOPA): ok

## Regressiecheck (aangroei t.o.v. eerdere runs)
- geen afwijkingen ten opzichte van eerdere runs

## Fouten
- geen
