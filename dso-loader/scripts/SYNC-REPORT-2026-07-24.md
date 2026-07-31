# Sync-rapport 2026-07-24

**Duur:** 7.5 uur · **fouten:** 0 · run_id 4
## Uitgangssituatie
- DB-grootte vooraf: 80 GB
- regelingen vooraf: 2151
- ALA-rijen vooraf: 396645
- schijf vrij: 415 GB

## Snapshot & dedup
- run_id: 4 (label `sync-2026-07-24-delta`)
- regeling_load-snapshot: 2136 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## p2p (Ow-regelingen)
- 1 bronhouders met nieuwe regelingen sinds 2026-07-15T13:29:49Z (rest ongewijzigd)
- fouten: 0

## i2a (IMTR toepasbare regels)
- 343/343 ok, fouten: 0

## vth (vergunningen)
- KOOP-kennisgevingen 2026-07-18..2026-07-24: load ok, enrich ok, geometrie-backfill ok
- DSO-afwijkvergunningen (BOPA): ok

## Post-processing
- nieuw geladen regelingen deze run: 15
- DB-grootte na sync: 80 GB
- regelingen inactief/totaal: 185/2151
- v_data_health: [{'bronhouders': 511, 'bronhouders_met_content': 381, 'code_only_met_content': 0, 'duplicate_naam': 0, 'pdok_mismatch': 0, 'regelingen_zonder_tekst': Decimal('0'), 'dso_mist_totaal': 0, 'avg_artikel_dekking_pct': Decimal('37.6'), 'avg_pct_brede_scope': Decimal('95.5'), 'avg_pct_anders_geduid': Decimal('85.8')}]

## Embeddings
- run_overnight.py: ok

## Fouten
- geen
