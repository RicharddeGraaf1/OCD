# Sync-rapport 2026-10-16

**Duur:** 1.8 uur · **fouten:** 0 · run_id 54
## Uitgangssituatie
- DB-grootte vooraf: 98 GB
- regelingen vooraf: 2111
- ALA-rijen vooraf: 402402
- schijf vrij: 447 GB

## Snapshot & dedup
- run_id: 54 (label `sync-2026-10-16-vth`)
- regeling_load-snapshot: 2111 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## vth (vergunningen)
- KOOP-kennisgevingen 2026-10-08..2026-10-16: load ok (geometrie-backfill draait in post)
- enrich-koop: ok · 73.0 min · +9,845 kennisgevingen · 2.2 kennisgevingen/s
- DSO-afwijkvergunningen (BOPA): ok

## Regressiecheck (aangroei t.o.v. eerdere runs)
- geen afwijkingen ten opzichte van eerdere runs

## Fouten
- geen
