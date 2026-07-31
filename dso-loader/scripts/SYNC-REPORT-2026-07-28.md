# Sync-rapport 2026-07-28
> **Doelwit-DB:** PRODUCTIE (direct)
**Duur:** 2.5 uur · **fouten:** 0 · run_id 5

## Uitgangssituatie
- DB-grootte vooraf: 55 GB
- regelingen vooraf: 1974
- ALA-rijen vooraf: 383793
- schijf vrij: 398 GB

## Snapshot & dedup
- run_id: 5 (label `prod-delta-2026-07-28`)
- regeling_load-snapshot: 1974 rijen → audit.regeling_load_hist
- bronhouder-snapshot: 511 rijen → audit.bronhouder_status_hist
- health-snapshot: 511 rijen → audit.bronhouder_health_hist
- ALA-dubbelgroepen na dedup: 0 (hoort 0 te zijn)

## p2p (Ow-regelingen)
- 1 bronhouders met nieuwe regelingen sinds 2026-07-22T04:17:02Z (rest ongewijzigd)
- fouten: 0

## Post-processing
- nieuw geladen regelingen deze run: 1
- DB-grootte na sync: 55 GB
- regelingen inactief/totaal: 8/1975
- v_data_health: [{'bronhouders': 511, 'bronhouders_met_content': 381, 'code_only_met_content': 0, 'duplicate_naam': 0, 'pdok_mismatch': 0, 'regelingen_zonder_tekst': Decimal('0'), 'dso_mist_totaal': 0, 'avg_artikel_dekking_pct': Decimal('42.3'), 'avg_pct_brede_scope': Decimal('95.3'), 'avg_pct_anders_geduid': Decimal('85.8')}]

## Fouten
- geen
