# Sync-rapport 2026-08-01
> **Doelwit-DB:** PRODUCTIE (direct)
**Duur:** 3.8 uur · **fouten:** 1 · run_id 6

## Uitgangssituatie
- DB-grootte vooraf: 55 GB
- regelingen vooraf: 1975
- ALA-rijen vooraf: 383793
- schijf vrij: 426 GB

## Snapshot & dedup
- run_id: 6 (label `prod-delta-2026-08-01`)
- regeling_load-snapshot: 1975 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## p2p (Ow-regelingen)
- 112 bronhouders met nieuwe regelingen sinds 2026-06-01T00:00:00Z (rest ongewijzigd)
- fouten: 0

## Post-processing
- nieuw geladen regelingen deze run: 15
- DB-grootte na sync: 57 GB
- regelingen inactief/totaal: 13/1990
- v_data_health: [{'bronhouders': 511, 'bronhouders_met_content': 381, 'code_only_met_content': 0, 'duplicate_naam': 0, 'pdok_mismatch': 0, 'regelingen_zonder_tekst': Decimal('0'), 'dso_mist_totaal': 0, 'avg_artikel_dekking_pct': Decimal('42.0'), 'avg_pct_brede_scope': Decimal('95.3'), 'avg_pct_anders_geduid': Decimal('85.7')}]

## Fouten
- drieslag-MV-refresh: timeout na 10800s
