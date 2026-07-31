# Sync-rapport 2026-07-17

**Duur:** 5.0 uur · **fouten:** 2 · run_id 1
## Uitgangssituatie
- DB-grootte vooraf: 76 GB
- regelingen vooraf: 2098
- ALA-rijen vooraf: 646688
- schijf vrij: 83 GB

## Snapshot & dedup
- run_id: 1 (label `full-sync-2026-07-17-avond`)
- regeling_load-snapshot: 1924 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## p2p (Ow-regelingen)
- 379/381 bronhouders ok
- fouten: 2 — 0225, 0971

## i2a (IMTR toepasbare regels)
- 343/343 ok, fouten: 0

## vth (KOOP-vergunningen)
- bereik 2026-07-14 .. 2026-07-17: load ok, enrich ok, geometrie-backfill ok

## Post-processing
- nieuw geladen regelingen deze run: 212
- DB-grootte na sync: 78 GB
- regelingen inactief/totaal: 178/2136
- v_data_health: [{'bronhouders': 511, 'bronhouders_met_content': 381, 'code_only_met_content': 0, 'duplicate_naam': 0, 'pdok_mismatch': 0, 'regelingen_zonder_tekst': Decimal('0'), 'dso_mist_totaal': 0, 'avg_artikel_dekking_pct': Decimal('37.7'), 'avg_pct_brede_scope': Decimal('95.5'), 'avg_pct_anders_geduid': Decimal('86.0')}]

## Embeddings
- run_overnight.py: ok

## Fouten
- p2p 0225: error: Server error '503 Service Unavailable' for url 'https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8/regelingen?bevoegdGezag=gm0225&page=9&size=200'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- p2p 0971: error: Server error '503 Service Unavailable' for url 'https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8/regelingen?bevoegdGezag=gm0971&page=6&size=200'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
