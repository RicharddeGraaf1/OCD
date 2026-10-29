# Sync-rapport 2026-10-07

**Duur:** 1.2 uur · **fouten:** 0 · run_id 51
## Uitgangssituatie
- DB-grootte vooraf: 97 GB
- regelingen vooraf: 2091
- ALA-rijen vooraf: 398149
- schijf vrij: 446 GB

## Snapshot & dedup
- run_id: 51 (label `sync-2026-10-07-vth`)
- regeling_load-snapshot: 2091 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## vth (vergunningen)
- KOOP-kennisgevingen 2026-10-02..2026-10-07: load ok (geometrie-backfill draait in post)
- enrich-koop: ok · 41.4 min · +5,328 kennisgevingen · 2.1 kennisgevingen/s
- DSO-afwijkvergunningen (BOPA): ok

## Regressiecheck (aangroei t.o.v. eerdere runs)
- geen afwijkingen ten opzichte van eerdere runs

## Fouten
- geen
