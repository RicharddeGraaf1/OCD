# Sync-rapport 2026-08-08

**Duur:** 5.6 uur · **fouten:** 1 · run_id 7
## Uitgangssituatie
- DB-grootte vooraf: 79 GB
- regelingen vooraf: 2000
- ALA-rijen vooraf: 384391
- schijf vrij: 414 GB

## Snapshot & dedup
- run_id: 7 (label `i2a-na-prefixfix`)
- regeling_load-snapshot: 2000 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## i2a (IMTR toepasbare regels)
- 342/343 ok, fouten: 1

## Fouten
- i2a 1699: error: Server error '503 Service Unavailable' for url 'https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/rtrgegevens/v2/activiteiten/_zoek'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
